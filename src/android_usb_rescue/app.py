"""Mouse-to-AOA absolute touch with explicitly dated static references.

No video mirroring, driver changes, USB reset, automatic tapping or typing.
Only explicit user touch/button actions are forwarded. --dry-run never opens
USB. Pointer coordinates stay in memory and are not logged to disk.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime
import json
import hashlib
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time

DPI_AWARENESS = None
from PIL import Image, ImageTk
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from .settings import get_data_dir, load_config, save_config, parse_usb_id
from . import __version__


def map_point(x, y, origin, drawn, source):
    """Canvas content coordinates -> normalized phone point and source pixel."""
    if not all(math.isfinite(v) for v in (x, y, *origin, *drawn, *source)) or min(*drawn, *source) <= 0:
        return None
    px, py = x - origin[0], y - origin[1]
    if not (0 <= px < drawn[0] and 0 <= py < drawn[1]):
        return None
    nx = px / max(1, drawn[0] - 1)
    ny = py / max(1, drawn[1] - 1)
    return nx, ny, round(nx * (source[0] - 1)), round(ny * (source[1] - 1))


class UsbWorker(threading.Thread):
    def __init__(self, events, target, screenshot_dir, dry_run=False, session_factory=None):
        super().__init__(name="android-usb-rescue-worker", daemon=False)
        self.target = dict(target)
        self.screenshot_dir = Path(screenshot_dir)
        self.session_factory = session_factory
        self.events = events
        self.commands = queue.Queue(maxsize=128)
        self.stop_requested = threading.Event()
        self.release_requested = threading.Event()
        self.generation = 0
        self.dry_run = dry_run

    def cancel_input(self):
        self.generation += 1
        self.release_requested.set()

    def run(self):
        session = None
        active = False
        point = (0.5, 0.5)
        touched = 0.0
        touch_started = 0.0

        def release():
            nonlocal active
            if session and active:
                try:
                    session.contact(False, *point)
                finally:
                    session.send(57, data=bytes(7), hid_id=session.hid_id + 1)
            active = False

        try:
            if not self.dry_run:
                from .usb_transport import Session
                factory = self.session_factory or Session
                session = factory(**self.target)
                session.register()
                session.register_touch()
            self.events.put(('ready', '演示模式：不向手机发送输入' if self.dry_run else
                             'USB 已连接；实际点击位置仍需手机反馈确认'))
            while not self.stop_requested.is_set():
                if self.release_requested.is_set():
                    release()
                    self.release_requested.clear()
                if active and time.monotonic() - touch_started > 4:
                    self.generation += 1
                    release()
                    self.events.put(('message', '已自动松开超过 4 秒的触点'))
                try:
                    action, data, generation = self.commands.get(timeout=0.05)
                except queue.Empty:
                    if active and time.monotonic() - touched > 4:
                        release()
                        self.events.put(('message', '已自动松开超过 4 秒的触点'))
                    continue
                if self.release_requested.is_set():
                    release()
                    self.release_requested.clear()
                if action != 'files' and generation != self.generation:
                    if action in ('button', 'refresh'):
                        self.events.put(('cancelled', '操作已取消；鼠标控制保持暂停'))
                    continue
                if action in ('down', 'move'):
                    if action == 'move' and not active:
                        continue
                    if action == 'down':
                        release()
                        touch_started = time.monotonic()
                    point = data
                    if session:
                        session.contact(True, *point)
                    active = True
                    touched = time.monotonic()
                elif action == 'up':
                    release()
                elif action == 'button':
                    release()
                    if session:
                        if data == 'home':
                            session.consumer_key(0x223)
                        elif data == 'back':
                            session.key(0x29)
                        elif data == 'settings':
                            session.key(0x0c, 8)
                        elif data == 'screenshot':
                            session.key(0x46)
                            if self.stop_requested.wait(1):
                                break
                    self.events.put(('button_done', '演示模式：未发送按键' if self.dry_run else '按键已发送；请刷新截图确认手机页面'))
                elif action in ('files', 'refresh'):
                    release()
                    if self.dry_run:
                        self.events.put(('files', {'ok': False, 'error': '演示模式不读取手机'}))
                        continue
                    if action == 'refresh':
                        session.key(0x46)
                        if self.stop_requested.wait(1):
                            break
                    from .mtp_reader import probe, latest_screenshot
                    report = probe(session)
                    if report.get('storage_accessible'):
                        report = latest_screenshot(session, self.screenshot_dir)
                    report['capture_requested']=action=='refresh'
                    self.events.put(('files', report))
        except Exception as error:
            self.events.put(('failed', str(error)))
        finally:
            try:
                release()
            except Exception:
                pass
            if session:
                try:
                    session.close()
                except Exception:
                    pass
            self.events.put(('closed', 'USB 已释放'))


class ReferenceApp:
    def __init__(self, root, dry_run=False, start_paused=True, reference=None, *, data_dir=None, config=None, enable_local_api=False):
        from .local_api import LocalControlApi
        from .reference_catalog import load_references
        self.root = root
        self.dry_run = dry_run
        self.start_paused = start_paused
        self.data_dir = get_data_dir(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config = dict(load_config(self.data_dir) if config is None else config)
        self.local_api = LocalControlApi(self.data_dir, os.getpid()) if enable_local_api else None
        self.serial = tk.StringVar(value=self.config.get("serial", ""))
        self.pending_reconnect = False
        self.last_error = None
        self.action_buttons = []
        self.last_file_check = None
        self.last_status_write = 0.0
        self.events = queue.Queue()
        self.worker = None
        self.ready = False
        self.busy = False
        self.dragging = False
        self.last_motion = 0.0
        self.point = None
        self.closed = False
        self.origin = (0, 0)
        self.drawn = (1, 1)
        self.photo = None
        self.image = None
        self.image_path = None
        self.source_kind = '历史截图'
        self.render_timer = None
        self.enabled = tk.BooleanVar(value=False)
        self.grid = tk.BooleanVar(value=False)
        self.mode = tk.StringVar(value='100% 原图像素（需滚动）' if self.config.get('view') == 'original' else '完整等比显示')
        self.message = tk.StringVar(value='请选择参考图，填写手机序列号后手动连接 USB')
        self.position = tk.StringVar(value='鼠标停在图片内显示参考坐标')
        self.banner = tk.StringVar(value='尚未加载参考图 ｜ 非实时画面')

        root.title('Android USB Rescue — 静态截图，非实时画面' + ('（演示）' if dry_run else ''))
        root.geometry(f'{max(640,min(1240, root.winfo_screenwidth()-80))}x{max(540,min(1180, root.winfo_screenheight()-100))}+40+30')
        root.minsize(640, 540)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.bind('<Escape>', self.pause)
        root.bind('<FocusOut>', lambda event: root.after_idle(self.release_if_unfocused))

        ttk.Label(root, textvariable=self.banner, foreground='#a62317',
                  font=('Microsoft YaHei UI', 10, 'bold')).pack(fill='x', padx=12, pady=(9, 3))
        ttk.Label(root, text='旧图只作定位参考。左键点按／拖动发送触摸；Esc 暂停。滚轮仅滚动电脑参考图。').pack(fill='x', padx=12)

        connection = ttk.Frame(root)
        connection.pack(fill='x', padx=10, pady=(8,0))
        ttk.Label(connection,text='手机序列号').pack(side='left')
        self.serial_entry=ttk.Entry(connection,textvariable=self.serial,width=30)
        self.serial_entry.pack(side='left',padx=6)
        self.connect_button=ttk.Button(connection,text='连接 USB',command=self.connect)
        self.connect_button.pack(side='left',padx=4)
        self.disconnect_button=ttk.Button(connection,text='断开',command=self.disconnect)
        self.disconnect_button.pack(side='left',padx=4)
        ttk.Label(connection,text='连接前需要正确的 USB 参数；取图后手动恢复鼠标控制').pack(side='left',padx=8)

        bar = ttk.Frame(root)
        bar.pack(fill='x', padx=10, pady=7)
        self.toggle = ttk.Checkbutton(bar, text='启用鼠标控制手机', variable=self.enabled, command=self.toggle_control)
        self.toggle.pack(side='left', padx=(0, 12))
        self.view = ttk.Combobox(bar, state='readonly', textvariable=self.mode,
                                values=('完整等比显示', '100% 原图像素（需滚动）'), width=23)
        self.view.pack(side='left', padx=4)
        self.view.bind('<<ComboboxSelected>>', self.change_view)
        self.grid_toggle=ttk.Checkbutton(bar, text='10% 网格', variable=self.grid, command=self.render)
        self.grid_toggle.pack(side='left', padx=8)
        self.reload_button=ttk.Button(bar, text='刷新图片列表', command=self.reload_references)
        self.reload_button.pack(side='left', padx=4)
        self.refresh_button=ttk.Button(bar,text='截图并刷新',command=self.refresh_screenshot)
        self.refresh_button.pack(side='right')
        self.action_buttons.append(self.refresh_button)

        second = ttk.Frame(root)
        second.pack(fill='x', padx=10, pady=(0, 7))
        rows=load_references(self.data_dir)
        self.paths=[Path(row['path']).resolve() for row in rows]
        self.reference_meta={Path(row['path']).resolve():row for row in rows}
        self.archive_dir=self.data_dir/'references'
        self.archive_dir.mkdir(exist_ok=True)
        default_reference=0
        self.reference = ttk.Combobox(second, values=[row['label'] for row in rows], state='readonly', width=42)
        if self.paths:
            self.reference.current(default_reference)
        self.reference.pack(side='left', padx=(0, 12))
        self.reference.bind('<<ComboboxSelected>>', self.select_reference)
        self.choose_button=ttk.Button(second,text='选择图片',command=self.choose_image)
        self.choose_button.pack(side='left',padx=3)
        for title, action in [('手机主页', 'home'), ('返回', 'back'), ('打开设置', 'settings'), ('手机截图', 'screenshot')]:
            button=ttk.Button(second, text=title, command=lambda name=action: self.button(name))
            button.pack(side='left', padx=3)
            self.action_buttons.append(button)
        self.files_button=ttk.Button(second, text='检查文件／取新截图', command=self.files)
        self.files_button.pack(side='right')
        self.action_buttons.append(self.files_button)

        frame = ttk.Frame(root)
        frame.pack(fill='both', expand=True, padx=10)
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(frame, background='#20252d', highlightthickness=0, xscrollincrement=1, yscrollincrement=1)
        self.canvas.grid(row=0, column=0, sticky='nsew')
        self.ys = ttk.Scrollbar(frame, orient='vertical', command=self.canvas.yview)
        self.xs = ttk.Scrollbar(frame, orient='horizontal', command=self.canvas.xview)
        self.ys.grid(row=0, column=1, sticky='ns')
        self.xs.grid(row=1, column=0, sticky='ew')
        self.canvas.configure(xscrollcommand=self.xs.set, yscrollcommand=self.ys.set)
        self.canvas.bind('<Configure>', self.defer_render)
        self.canvas.bind('<Motion>', self.motion)
        self.canvas.bind('<ButtonPress-1>', self.press)
        self.canvas.bind('<ButtonRelease-1>', self.release)
        self.canvas.bind('<Leave>', self.cancel_drag)
        self.canvas.bind('<MouseWheel>', self.wheel)
        self.canvas.bind('<Shift-MouseWheel>', lambda e: self.wheel(e, horizontal=True))
        ttk.Label(root, textvariable=self.position).pack(fill='x', padx=12, pady=(5, 0))
        ttk.Label(root, textvariable=self.message, foreground='#176d58', wraplength=1180).pack(fill='x', padx=12, pady=(2, 8))
        if self.paths:
            self.try_load(self.paths[default_reference])
        selected=reference or self.config.get('reference')
        if selected:
            self.try_load(selected)
        self._sync_controls()
        self.render()
        root.after(100,self.poll)

    def _save_preferences(self):
        self.config.update(serial=self.serial.get().strip(),
                           reference=str(self.image_path) if self.image_path else '',
                           view='original' if self.mode.get().startswith('100%') else 'fit')
        try:
            save_config(self.data_dir,self.config)
        except OSError:
            self.message.set('本机设置未能保存；当前操作仍可继续')

    def try_load(self,path,kind='本地参考图',recorded_at=None):
        try:
            self.load(path,kind,recorded_at)
            return True
        except (OSError,ValueError,SyntaxError,Image.DecompressionBombError):
            self.message.set('无法加载图片：请选择有效的竖屏手机图片')
            return False

    def load(self, path, kind='本地参考图',recorded_at=None):
        selected=Path(path).expanduser().resolve()
        with Image.open(selected) as image:
            if image.width<=0 or image.height<=image.width or image.width*image.height>40_000_000:
                raise ValueError('Expected a reasonably sized portrait phone image')
            converted=image.convert('RGB')
        self.pause()
        self.image_path=selected
        self.image=converted
        meta=self.reference_meta.get(self.image_path,{})
        if kind in ('历史截图','本地参考图'):
            kind=meta.get('kind',kind)
        self.source_kind = kind
        stamp = datetime.fromtimestamp(self.image_path.stat().st_mtime).strftime('%Y-%m-%d %H:%M')
        stamp_label='本地文件时间'
        timestamp=recorded_at or meta.get('recorded_at')
        if timestamp:
            try:
                moment=datetime.fromisoformat(timestamp.replace('Z','+00:00'))
                if moment.tzinfo is not None:
                    stamp=moment.astimezone().strftime('%Y-%m-%d %H:%M:%S')
                    stamp_label='记录时间（本机时区）'
                else:
                    stamp=moment.strftime('%Y-%m-%d %H:%M:%S')
                    stamp_label='手机截图时间' if recorded_at else '历史记录时间'
            except (ValueError,TypeError):
                pass
        self.banner.set(f'{kind}，非实时画面 ｜ {self.image.width}×{self.image.height} ｜ {stamp_label} {stamp}')
        if self.image_path in self.paths:
            self.reference.current(self.paths.index(self.image_path))
        else:
            self.reference.set(f'{kind} · {stamp}')
        self.render()
        self._sync_controls()
        self._save_preferences()

    def select_reference(self,event=None):
        index=self.reference.current()
        if 0<=index<len(self.paths):
            self.try_load(self.paths[index])

    def change_view(self,event=None):
        self.render()
        self._save_preferences()

    def reload_references(self):
        from .reference_catalog import load_references
        if self.busy:
            return
        self.pause()
        selected=self.image_path
        rows=load_references(self.data_dir)
        self.paths=[Path(row['path']).resolve() for row in rows]
        self.reference_meta={Path(row['path']).resolve():row for row in rows}
        self.reference.configure(values=[row['label'] for row in rows])
        if selected in self.paths:
            self.reference.current(self.paths.index(selected))
        elif self.image is not None:
            self.reference.set(self.source_kind+' · 当前图片')
        elif self.paths:
            self.try_load(self.paths[0])
        else:
            self.reference.set('尚无参考图片')
        self._sync_controls()
        self.message.set(f'已载入 {len(self.paths)} 张参考图；鼠标控制已暂停')
        self.write_status()

    def choose_image(self):
        if self.busy:
            return
        self.pause()
        path=filedialog.askopenfilename(parent=self.root,initialdir=str(self.archive_dir),
            title='选择竖屏手机图片作为静态参考',filetypes=[('图片','*.png *.jpg *.jpeg *.webp *.bmp')])
        if path and self.try_load(path):
            self.message.set('已加载本地参考图；确认手机页面后可手动启用鼠标控制')

    def defer_render(self, event=None):
        if self.closed:
            return
        if self.render_timer:
            self.root.after_cancel(self.render_timer)
        self.render_timer = self.root.after(100, self.render)

    def render(self):
        self.render_timer = None
        self.cancel_input()
        if self.closed:
            return
        if self.image is None:
            self.canvas.delete('all')
            self.ys.grid_remove()
            self.xs.grid_remove()
            self.canvas.create_text(max(1,self.canvas.winfo_width())//2,
                                    max(1,self.canvas.winfo_height())//2,
                                    text='选择一张竖屏手机截图开始\n\n静态图仅作定位参考；不会自动连接 USB',
                                    fill='#d7e2ec',width=max(150,self.canvas.winfo_width()-50),
                                    font=('Microsoft YaHei UI',13))
            self.write_status()
            return
        if self.mode.get().startswith('100%'):
            self.ys.grid()
            self.xs.grid()
        else:
            self.ys.grid_remove()
            self.xs.grid_remove()
        cw, ch = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        scale = 1.0 if self.mode.get().startswith('100%') else min(max(1,cw-12)/self.image.width, max(1,ch-12)/self.image.height, 1.0)
        w, h = max(1, round(self.image.width*scale)), max(1, round(self.image.height*scale))
        self.drawn = (w, h)
        self.origin = (max(0, (cw-w)//2), max(0, (ch-h)//2))
        rendered = self.image if scale == 1.0 else self.image.resize((w, h), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(rendered)
        self.canvas.delete('all')
        ox, oy = self.origin
        self.canvas.create_image(ox, oy, anchor='nw', image=self.photo)
        self.canvas.configure(scrollregion=(0, 0, max(cw,w), max(ch,h)))
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        if self.grid.get():
            for step in range(1,10):
                x, y = ox+(w-1)*step/10, oy+(h-1)*step/10
                self.canvas.create_line(x,oy,x,oy+h-1,fill='#10a7ad',dash=(3,5))
                self.canvas.create_line(ox,y,ox+w-1,y,fill='#10a7ad',dash=(3,5))
        self.write_status()

    def event_point(self, event):
        if self.image is None:
            return None
        return map_point(self.canvas.canvasx(event.x), self.canvas.canvasy(event.y),
                         self.origin, self.drawn, self.image.size)

    def motion(self, event):
        point = self.event_point(event)
        self.point = point
        self.canvas.delete('crosshair')
        if point:
            nx,ny,x,y=point
            self.position.set(f'参考原图 X={x}, Y={y} ｜ 横向 {nx*100:.2f}% · 纵向 {ny*100:.2f}% ｜ 显示 {self.drawn[0]/self.image.width*100:.1f}%')
            cx,cy=self.canvas.canvasx(event.x),self.canvas.canvasy(event.y)
            self.canvas.create_line(cx-8,cy,cx+8,cy,fill='#ec174d',tags='crosshair')
            self.canvas.create_line(cx,cy-8,cx,cy+8,fill='#ec174d',tags='crosshair')
            if self.dragging and time.monotonic()-self.last_motion>=0.033:
                self.last_motion=time.monotonic()
                self.send('move',(nx,ny))
        elif self.dragging:
            self.cancel_input()

    def press(self, event):
        if not (self.ready and self.enabled.get() and not self.busy and self.image is not None):
            return
        point=self.event_point(event)
        if point:
            self.canvas.focus_set()
            self.dragging=True
            self.send('down',point[:2])

    def release(self, event=None):
        if self.dragging:
            self.dragging=False
            self.send('up',None)

    def release_if_unfocused(self):
        if self.closed:
            return
        # ttk popdown windows have Tcl names but no tkinter widget wrapper.
        try:
            focused=self.root.tk.call('focus','-displayof',self.root._w)
        except tk.TclError:
            focused=''
        if not focused:
            self.cancel_input()

    def cancel_drag(self, event=None):
        if self.dragging:
            self.cancel_input()

    def cancel_input(self):
        self.dragging=False
        if self.worker and self.worker.is_alive():
            self.worker.cancel_input()

    def pause(self, event=None):
        self.cancel_input()
        self.enabled.set(False)
        self.message.set('已暂停手机鼠标输入；可勾选重新启用')
        self.write_status()

    def toggle_control(self):
        self.cancel_input()
        if not self.ready or self.busy or self.image is None:
            self.enabled.set(False)
        self.message.set('鼠标控制已启用；只有图片内的左键点按／拖动会发送' if self.enabled.get() else '鼠标控制已暂停')
        self.write_status()

    def wheel(self, event, horizontal=False):
        self.release()
        amount=-int(event.delta/120)*80
        (self.canvas.xview_scroll if horizontal else self.canvas.yview_scroll)(amount,'units')
        return 'break'

    def send(self, action, data):
        if not self.worker or not self.worker.is_alive() or self.worker.stop_requested.is_set():
            return False
        try:
            self.worker.commands.put_nowait((action,data,self.worker.generation))
            return True
        except queue.Full:
            self.enabled.set(False)
            self.dragging=False
            self.worker.cancel_input()
            self.worker.stop_requested.set()
            self.ready=False
            self.message.set('输入队列拥堵，正在释放连接；请重新连接')
            self._sync_controls()
            return False

    def button(self, action):
        if not self.ready or self.busy or action not in ('home','back','settings','screenshot'):
            return False
        self.pause()
        self.busy=True
        accepted=self.send('button',action)
        if not accepted:
            self.busy=False
        self._sync_controls()
        self.write_status()
        return accepted

    def capture_screenshot(self):
        return self.button('screenshot')

    def _file_action(self,action):
        if not self.ready or self.busy:
            return False
        self.pause()
        self.busy=True
        self.message.set('正在截图并读取手机存储…' if action=='refresh' else '正在只读检查手机存储…')
        accepted=self.send(action,None)
        if not accepted:
            self.busy=False
        self._sync_controls()
        self.write_status()
        return accepted

    def files(self):
        return self._file_action('files')

    def refresh_screenshot(self):
        return self._file_action('refresh')

    def _target(self):
        serial=self.serial.get().strip()
        if not serial or len(serial)>256 or not serial.isascii():
            raise ValueError('请填写不超过256字符的手机USB序列号')
        return {'serial':serial,'vendor_id':parse_usb_id(self.config.get('vendor_id','2717')),
                'product_id':parse_usb_id(self.config.get('product_id','ff40')),
                'libusb_path':self.config.get('libusb_path') or None}

    def _worker_alive(self):
        return self.worker is not None and self.worker.is_alive()

    def connect(self):
        if self.closed or self.busy:
            return False
        try:
            target=self._target()
        except (ValueError,TypeError) as error:
            self.message.set(str(error))
            return False
        self.pause()
        self._save_preferences()
        if self._worker_alive():
            self.pending_reconnect=True
            self.worker.stop_requested.set()
            self.ready=False
            self.message.set('正在释放原连接，随后重新连接…')
        else:
            self._start_worker(target)
        self._sync_controls()
        return True

    def _start_worker(self,target=None):
        if self._worker_alive():
            return
        try:
            target=target or self._target()
        except (ValueError,TypeError) as error:
            self.message.set(str(error))
            return
        self.pending_reconnect=False
        self.last_error=None
        self.ready=False
        self.enabled.set(False)
        self.worker=UsbWorker(self.events,target,self.data_dir/'screenshots',self.dry_run)
        self.message.set('正在验证指定设备并注册输入通道…')
        self.worker.start()

    def disconnect(self):
        self.pending_reconnect=False
        self.pause()
        self.ready=False
        if self._worker_alive():
            self.worker.stop_requested.set()
            self.message.set('正在结束当前操作并释放USB…')
        else:
            self.message.set('USB已断开')
        self._sync_controls()

    def _sync_controls(self):
        active=self._worker_alive()
        idle=not self.busy and not self.closed
        phone_idle=idle and self.ready
        for button in self.action_buttons:
            button.configure(state='normal' if phone_idle else 'disabled')
        self.toggle.configure(state='normal' if phone_idle and self.image is not None else 'disabled')
        self.connect_button.configure(text='重新连接' if active else '连接USB',state='normal' if idle else 'disabled')
        self.disconnect_button.configure(state='normal' if active and not self.closed else 'disabled')
        self.serial_entry.configure(state='normal' if idle and not active else 'disabled')
        self.reference.configure(state='readonly' if idle and self.paths else 'disabled')
        self.choose_button.configure(state='normal' if idle else 'disabled')
        self.reload_button.configure(state='normal' if idle else 'disabled')
        self.view.configure(state='readonly' if idle else 'disabled')
        self.grid_toggle.configure(state='normal' if idle else 'disabled')
        if not phone_idle or self.image is None:
            self.enabled.set(False)

    def poll(self):
        try:
            while True:
                kind,data=self.events.get_nowait()
                if kind=='ready':
                    self.ready=True
                    self.enabled.set(False)
                    self.message.set(data)
                elif kind=='failed':
                    self.ready=False
                    self.enabled.set(False)
                    self.busy=False
                    self.dragging=False
                    self.last_error=data
                    self.message.set('USB操作停止：'+data+'。可断开或重新连接。')
                elif kind=='closed':
                    self.ready=False
                    self.busy=False
                    self.enabled.set(False)
                    if not self.last_error and not self.pending_reconnect:
                        self.message.set('USB已释放')
                elif kind=='files':
                    self.busy=False
                    previous=self.last_file_check
                    self.last_file_check={'time':time.time(),**data}
                    identity=(data.get('screenshot_object_handle'),data.get('screenshot_time'))
                    unchanged=(data.get('capture_requested') and previous and identity[0] is not None
                               and identity==(previous.get('screenshot_object_handle'),previous.get('screenshot_time')))
                    if unchanged:
                        self.message.set('未检测到新生成的截图；保留原图，请确认手机已唤醒并核对截图时间')
                    elif data.get('ok') and data.get('screenshot_path'):
                        if self.try_load(data['screenshot_path'],'手机取回的静态截图',data.get('screenshot_time')):
                            self.message.set('已读取手机保存的最新截图；请核对截图时间，查看后可手动启用控制')
                    elif data.get('storage_ids'):
                        self.message.set('手机存储已开放，尚未取到截图：'+data.get('error','读取未完成'))
                    elif data.get('ok') and not data.get('storage_accessible'):
                        self.message.set('手机仍返回空存储列表，尚不能读取截图。参考图保持不变。')
                    else:
                        self.message.set('未取到新截图：'+data.get('error','未知状态'))
                elif kind in ('button_done','cancelled'):
                    self.busy=False
                    self.message.set(data)
                elif kind=='message':
                    self.message.set(data)
                self._sync_controls()
                self.write_status()
        except queue.Empty:
            pass
        if self.closed:
            if not self.worker or not self.worker.is_alive():
                self.root.destroy()
                return
        else:
            if self.pending_reconnect and not self._worker_alive():
                self._start_worker()
                self._sync_controls()
            if self.local_api:
                self.local_api.poll(self.status_data(), self.files, self.capture_screenshot)
            if time.monotonic()-self.last_status_write>=2:
                self.write_status()
        self.root.after(80,self.poll)

    def status_data(self):
        data={'pid':os.getpid(),'time':time.time(),'connected':self.ready,
              'control_enabled':self.enabled.get(),'busy':self.busy,'closed':self.closed,
              'dry_run':self.dry_run,'source_kind':self.source_kind,
              'source':str(self.image_path) if self.image_path else None,'source_size':self.image.size if self.image else None,
              'drawn_size':self.drawn,'view':self.mode.get(),'ui_confirmed':False}
        data['dpi_awareness']=DPI_AWARENESS
        data['local_api_enabled']=self.local_api is not None
        data.update(local_api_version=1, message=self.message.get(),
                    banner=self.banner.get(), dragging=self.dragging,
                    last_file_check=self.last_file_check)
        data.update(reference_count=len(self.reference.cget('values')),
                    reference_label=self.reference.get(),
                    reference_sources={kind:sum(row.get('kind')==kind for row in self.reference_meta.values())
                                       for kind in {row.get('kind') for row in self.reference_meta.values()}})
        return data

    def write_status(self):
        data=self.status_data()
        path=self.data_dir/'status.json'
        try:
            temporary=path.with_suffix('.tmp')
            temporary.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
            temporary.replace(path)
            self.last_status_write=time.monotonic()
        except OSError:
            pass

    def close(self):
        if self.closed:
            return
        self.pending_reconnect=False
        self.cancel_input()
        self.closed=True
        self.enabled.set(False)
        self.message.set('正在释放 USB；完成后窗口会关闭…')
        if self.worker:
            self.worker.stop_requested.set()
        self._sync_controls()
        self.write_status()


def main():
    global DPI_AWARENESS
    parser=argparse.ArgumentParser(description='使用静态截图映射USB触摸，并通过MTP取回手机截图。不会自动连接USB。')
    parser.add_argument('--version',action='version',version='%(prog)s '+__version__)
    parser.add_argument('--dry-run',action='store_true',help='演示模式，不打开USB或发送手机输入')
    parser.add_argument('--paused',action='store_true',help='兼容选项；所有连接默认暂停鼠标控制')
    parser.add_argument('--reference',help='启动时加载的竖屏参考图片')
    parser.add_argument('--data-dir',help='本机设置、参考图和截图目录')
    parser.add_argument('--serial',help='必须精确匹配的USB设备序列号')
    parser.add_argument('--vid',help='USB厂商ID，十六进制，例如2717')
    parser.add_argument('--pid',help='USB产品ID，十六进制，例如ff40')
    parser.add_argument('--libusb',help='libusb动态库的完整路径，默认自动查找')
    parser.add_argument('--enable-local-api',action='store_true',help='显式启用本机文件接口，默认关闭')
    args=parser.parse_args()
    data_dir=get_data_dir(args.data_dir)
    config=load_config(data_dir)
    for value,key in [(args.serial,'serial'),(args.vid,'vendor_id'),(args.pid,'product_id'),(args.libusb,'libusb_path')]:
        if value is not None:
            config[key]=value
    for key in ('vendor_id','product_id'):
        try:
            parse_usb_id(config.get(key,''))
        except (TypeError,ValueError) as error:
            parser.error(str(error))
    mutex=None
    if sys.platform=='win32':
        ctypes.windll.kernel32.CreateMutexW.argtypes=(ctypes.c_void_p,ctypes.c_bool,ctypes.c_wchar_p)
        ctypes.windll.kernel32.CreateMutexW.restype=ctypes.c_void_p
        identity=hashlib.sha256(str(data_dir.resolve()).casefold().encode('utf-8')).hexdigest()[:24]
        mutex=ctypes.windll.kernel32.CreateMutexW(None,False,'Local\\AndroidUsbRescue-'+identity)
        if not mutex:
            raise OSError('Unable to create the application instance lock')
        if ctypes.windll.kernel32.GetLastError()==183:
            notice=tk.Tk()
            notice.withdraw()
            messagebox.showinfo('Android USB Rescue','控制窗口已经打开，请从任务栏切回原窗口。',parent=notice)
            notice.destroy()
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex))
            return
    if sys.platform=='win32':
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError,OSError):
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        ctypes.windll.user32.GetThreadDpiAwarenessContext.restype=ctypes.c_void_p
        context=ctypes.windll.user32.GetThreadDpiAwarenessContext()
        DPI_AWARENESS=ctypes.windll.user32.GetAwarenessFromDpiAwarenessContext(ctypes.c_void_p(context))
    try:
        root=tk.Tk()
        ReferenceApp(root,args.dry_run,True,args.reference,data_dir=data_dir,config=config,enable_local_api=args.enable_local_api)
        root.mainloop()
    finally:
        if mutex:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex))


if __name__=='__main__':
    main()
