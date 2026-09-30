from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Event

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, RoundedRectangle
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import ListProperty, StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.recycleview import RecycleView
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput
from kivy.utils import platform

from .instagram import InstagramReader, SyncError, make_client, safe_error
from .model import DataError, Member, Sample, Snapshot, username
from .store import Store
from .vault import SessionVault

BG = (0.047, 0.055, 0.078, 1)
CARD = (0.09, 0.102, 0.137, 1)
INK = (0.94, 0.95, 0.98, 1)
MUTED = (0.57, 0.61, 0.70, 1)
LIME = (0.75, 0.94, 0.39, 1)
PINK = (1.0, 0.48, 0.63, 1)


class Card(BoxLayout):
    def __init__(self, color=CARD, **kwargs):
        super().__init__(**kwargs)
        with self.canvas.before:
            Color(*color)
            self.rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(18)])
        self.bind(pos=self._draw, size=self._draw)

    def _draw(self, *_):
        self.rect.pos, self.rect.size = self.pos, self.size


class Pill(Button):
    fill = ListProperty(CARD)

    def __init__(self, **kwargs):
        kwargs.setdefault("color", INK)
        super().__init__(background_normal="", background_down="", background_color=(0, 0, 0, 0), font_size=dp(13), **kwargs)
        with self.canvas.before:
            self.tint = Color(*self.fill)
            self.rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(13)])
        self.bind(pos=self._draw, size=self._draw, fill=self._draw)

    def _draw(self, *_):
        self.tint.rgba = self.fill
        self.rect.pos, self.rect.size = self.pos, self.size


class EventRow(BoxLayout):
    title = StringProperty("")
    detail = StringProperty("")
    badge = StringProperty("−")
    tint = ListProperty(PINK)


Builder.load_string('''
<EventRow>:
    size_hint_y: None
    height: dp(80)
    spacing: dp(12)
    padding: dp(12), dp(8)
    canvas.before:
        Color:
            rgba: .09, .102, .137, 1
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(15)]
    Label:
        text: root.badge
        color: root.tint
        font_size: dp(25)
        size_hint_x: None
        width: dp(30)
    BoxLayout:
        orientation: 'vertical'
        Label:
            text: root.title
            color: .94, .95, .98, 1
            bold: True
            font_size: dp(14)
            text_size: self.size
            halign: 'left'
            valign: 'middle'
            shorten: True
            shorten_from: 'right'
        Label:
            text: root.detail
            color: .57, .61, .70, 1
            font_size: dp(10)
            text_size: self.size
            halign: 'left'
            valign: 'middle'
            shorten: False
''')


def label(text, *, size=14, color=INK, height=28, bold=False):
    widget = Label(text=text, font_size=dp(size), color=color, bold=bold, size_hint_y=None, height=dp(height), halign="left", valign="middle")
    widget.bind(size=lambda w, s: setattr(w, "text_size", s))
    return widget


def field(hint, text="", password=False):
    return TextInput(hint_text=hint, text=text, multiline=False, password=password, size_hint_y=None, height=dp(47), background_normal="", background_active="", background_color=CARD, foreground_color=INK, hint_text_color=MUTED, cursor_color=LIME, padding=[dp(12), dp(13)], font_size=dp(14))


def short_date(value):
    return datetime.fromisoformat(value).astimezone().strftime("%d.%m %H:%M") if value else "ещё не проверяли"


class PulseApp(App):
    title = "Pulse — Instagram tracker"

    def __init__(self, demo=False, screenshot=None, **kwargs):
        super().__init__(**kwargs)
        self.demo_mode = demo
        self.screenshot = screenshot
        self.client = None
        self.web_login = None
        self.history_all = False
        self.busy = False
        self.cancel = Event()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pulse")
        self.filter = ("followers", "removed")
        self.cursor = None
        self.page_stack = []
        self.paused = False
        self.stopping = False
        self.login_popup = None

    def build(self):
        if platform != "android":
            Window.size = (420, 850)
        Window.clearcolor = BG
        self.data_dir = Path(os.environ.get("PULSE_HOME") or self.user_data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / "preferences.json"
        try:
            self.prefs = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.prefs = {}
        self.vault = SessionVault(self.data_dir / "session.enc", platform == "android")
        self.real_store = Store(self.data_dir / "pulse.sqlite3")
        self.store = self.real_store
        if self.demo_mode:
            self.seed_demo()
        root = BoxLayout(orientation="vertical", padding=[dp(20), dp(16)], spacing=dp(9))
        with root.canvas.before:
            Color(*BG)
            backdrop = RoundedRectangle(pos=root.pos, size=root.size, radius=[0])
        root.bind(pos=lambda w, pos: setattr(backdrop, "pos", pos), size=lambda w, size: setattr(backdrop, "size", size))
        top = BoxLayout(size_hint_y=None, height=dp(34))
        top.add_widget(label("pulse", size=26, height=34, bold=True))
        self.mode_label = label("ДЕМО" if self.demo_mode else "INSTAGRAM", size=10, color=LIME, height=34)
        self.mode_label.halign = "right"
        top.add_widget(self.mode_label)
        root.add_widget(top)
        root.add_widget(label("Что изменилось?", size=27, height=40, bold=True))
        root.add_widget(label("История подписчиков", size=12, color=MUTED, height=20))
        target_row = BoxLayout(size_hint_y=None, height=dp(47), spacing=dp(8))
        self.target = field("Аккаунт для проверки", "demo_account" if self.demo_mode else self.prefs.get("target", ""))
        self.target.bind(on_text_validate=lambda *_: self.refresh())
        self.target.bind(focus=lambda _, focused: self.refresh() if not focused else None)
        target_row.add_widget(self.target)
        self.settings_btn = Pill(text="Настройки", size_hint_x=None, width=dp(98), on_release=lambda *_: self.settings_dialog())
        target_row.add_widget(self.settings_btn)
        root.add_widget(target_row)
        stats = BoxLayout(size_hint_y=None, height=dp(83), spacing=dp(9))
        self.stats = []
        for title, tint in (("Подписчиков", INK), ("Ушли", PINK), ("Без ответа", PINK)):
            card = Card(orientation="vertical", padding=[dp(12), dp(10)])
            value = label("—", size=25, color=tint, height=34, bold=True)
            card.add_widget(value)
            card.add_widget(label(title, size=11, color=MUTED, height=23))
            stats.add_widget(card)
            self.stats.append(value)
        root.add_widget(stats)
        self.sync_btn = Pill(text="Импортировать выгрузку", fill=LIME, color=BG, bold=True, size_hint_y=None, height=dp(48), on_release=lambda *_: self.import_export())
        root.add_widget(self.sync_btn)
        self.status = label("Импортируйте ZIP/JSON выгрузки Instagram.", size=12, color=MUTED, height=54)
        root.add_widget(self.status)
        tabs = BoxLayout(size_hint_y=None, height=dp(37), spacing=dp(6))
        self.tabs = []
        for text, filt in (("Отписки", ("followers", "removed")), ("Не взаимно", ("nonreciprocal", "current")), ("Новые", ("followers", "added"))):
            button = Pill(text=text, on_release=lambda _, f=filt: self.set_filter(f))
            tabs.add_widget(button)
            self.tabs.append((button, filt))
        root.add_widget(tabs)
        history_row = BoxLayout(size_hint_y=None, height=dp(29), spacing=dp(6))
        self.history_note = label("Последняя проверка", size=11, color=MUTED, height=29)
        history_row.add_widget(self.history_note)
        self.history_btn = Pill(text="Вся история", size_hint_x=None, width=dp(105), on_release=lambda *_: self.toggle_history())
        history_row.add_widget(self.history_btn)
        root.add_widget(history_row)
        self.rv = RecycleView()
        from kivy.uix.recycleboxlayout import RecycleBoxLayout
        layout = RecycleBoxLayout(default_size=(None, dp(80)), default_size_hint=(1, None), size_hint_y=None, orientation="vertical", spacing=dp(7))
        layout.bind(minimum_height=layout.setter("height"))
        self.rv.add_widget(layout)
        self.rv.viewclass = EventRow
        root.add_widget(self.rv)
        pager = BoxLayout(size_hint_y=None, height=dp(32), spacing=dp(8))
        self.prev_btn = Pill(text="Назад", on_release=lambda *_: self.page_previous())
        self.next_btn = Pill(text="Далее", on_release=lambda *_: self.page_next())
        pager.add_widget(self.prev_btn)
        pager.add_widget(self.next_btn)
        root.add_widget(pager)
        footer = BoxLayout(size_hint_y=None, height=dp(36), spacing=dp(8))
        self.connect_btn = Pill(text="Импортировать данные", on_release=lambda *_: self.import_export())
        footer.add_widget(self.connect_btn)
        footer.add_widget(Pill(text="Как работает", on_release=lambda *_: self.help_dialog()))
        root.add_widget(footer)
        self.refresh()
        Clock.schedule_interval(self.auto_tick, 30)
        if self.screenshot:
            Clock.schedule_once(lambda _: Clock.schedule_once(self.capture, 1.5), 0)
        elif not self.demo_mode:
            Clock.schedule_once(lambda _: self.restore_session(), 0.5)
        return root

    def capture(self, _):
        self.root.export_to_png(str(Path(self.screenshot).resolve()))
        self.stop()

    def save_prefs(self):
        temp = self.config_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.prefs), encoding="utf-8")
        os.replace(temp, self.config_path)

    def seed_demo(self):
        self.store = Store(self.data_dir / "demo-v2.sqlite3")
        if self.store.accounts():
            return
        names = ["anna.visual", "max.travels", "dasha.film", "ilya.design", "maria.sea", "alex.notes", "kate.studio", "nikita.jpg"]
        people = tuple(Member(name, str(i + 100)) for i, name in enumerate(names))
        following = people[:6] + (Member("studio.north", "950"),)
        self.store.ingest(Snapshot("demo_account", "2026-09-27T09:00:00Z", (Sample("followers", "", people, "id"), Sample("following", "", following, "id"))))
        self.store.ingest(Snapshot("demo_account", "2026-09-29T10:30:00Z", (Sample("followers", "", people[3:] + (Member("sofia.art", "900"),), "id"), Sample("following", "", following, "id"))))

    def message(self, text):
        if not self.stopping:
            self.status.text = text

    def progress(self, text):
        Clock.schedule_once(lambda _, t=text: self.message(t))

    def submit(self, work, done):
        if self.busy:
            return
        self.busy = True
        self.target.disabled = self.settings_btn.disabled = self.connect_btn.disabled = True
        self.sync_btn.disabled = True
        future = self.pool.submit(work)

        def complete(_):
            if self.stopping:
                return
            self.busy = False
            self.target.disabled = self.settings_btn.disabled = self.connect_btn.disabled = False
            self.sync_btn.disabled = False
            try:
                value = future.result()
            except DataError as exc:
                self.message(str(exc))
                return
            except Exception:
                self.message("Не удалось импортировать выгрузку.")
                return
            done(value)

        future.add_done_callback(lambda _: Clock.schedule_once(complete))

    def restore_session(self):
        return

    def sync(self):
        self.import_export()

    def auto_tick(self, _):
        return

    def refresh(self):
        if not hasattr(self, "rv"):
            return
        try:
            account = username(self.target.text)
        except DataError:
            account = "unconfigured"
        if getattr(self, "display_account", None) != account:
            self.cursor, self.page_stack = None, []
            self.display_account = account
        stats = self.store.summary(account)
        for widget, value in zip(self.stats, (stats["followers"], stats["latest_unfollowers"], stats["nonreciprocal"])):
            widget.text = "—" if value is None else f"{value:,}".replace(",", " ")
        for widget, filt in self.tabs:
            widget.fill = (0.20, 0.26, 0.15, 1) if self.filter == filt else CARD
            widget.color = LIME if self.filter == filt else MUTED
        reciprocal_view = self.filter[0] == "nonreciprocal"
        self.history_btn.disabled = reciprocal_view
        self.history_btn.text = "Последняя" if self.history_all else "Вся история"
        self.history_note.text = "Подписки без ответа" if reciprocal_view else ("Вся история изменений" if self.history_all else "Последняя проверка")
        if reciprocal_view:
            reciprocal = self.store.nonreciprocal(account, after=self.cursor, limit=81)
            rows = reciprocal["users"]
        else:
            rows = self.store.events(account, *self.filter, before=self.cursor, limit=81, latest=not self.history_all)
        self.visible_rows = rows[:80]
        self.next_btn.disabled = len(rows) <= 80
        self.prev_btn.disabled = not self.page_stack
        if reciprocal_view:
            self.rv.data = [{"title": "@" + row["username"], "detail": "Вы подписаны, ответной подписки нет\nПроверено: " + short_date(reciprocal["captured_at"]), "badge": "·", "tint": PINK} for row in self.visible_rows]
        else:
            self.rv.data = [{"title": "@" + row["username"], "detail": f"{short_date(row['since'])} - {short_date(row['until'])}\nМежду двумя проверками", "badge": "+" if row["direction"] == "added" else "−", "tint": LIME if row["direction"] == "added" else PINK} for row in self.visible_rows]
        if not self.rv.data:
            title, detail = "Пока нет изменений", "Изменения определяются по двум полным проверкам."
            if reciprocal_view:
                title = "Невзаимных подписок нет" if reciprocal["ready"] else "Нет полных данных"
                detail = "По последним сохранённым спискам." if reciprocal["ready"] else "Нужны подписчики и подписки из одной проверки."
            self.rv.data = [{"title": title, "detail": detail, "badge": "·", "tint": MUTED}]
        self.rv.scroll_y = 1
        if not self.busy:
            self.message(("Демо · вымышленные данные\n" if self.demo_mode else "") + f"Последние данные: {short_date(stats['last_seen'])}")

    def set_filter(self, filt):
        self.filter, self.cursor, self.page_stack = filt, None, []
        self.refresh()

    def toggle_history(self):
        self.history_all = not self.history_all
        self.cursor, self.page_stack = None, []
        self.refresh()

    def page_next(self):
        if self.visible_rows:
            self.page_stack.append(self.cursor)
            self.cursor = self.visible_rows[-1]["member_key"] if self.filter[0] == "nonreciprocal" else self.visible_rows[-1]["id"]
            self.refresh()

    def page_previous(self):
        if self.page_stack:
            self.cursor = self.page_stack.pop()
            self.refresh()

    def modal(self, title, widgets, height=560):
        body = BoxLayout(orientation="vertical", spacing=dp(10), padding=dp(12), size_hint_y=None)
        body.bind(minimum_height=body.setter("height"))
        for widget in widgets:
            body.add_widget(widget)
        scroll = ScrollView()
        scroll.add_widget(body)
        popup = Popup(title=title, content=scroll, size_hint=(0.94, None), height=min(dp(height), Window.height * .92), background_color=BG, auto_dismiss=False)
        close = Pill(text="Закрыть", size_hint_y=None, height=dp(43), on_release=lambda *_: popup.dismiss())
        body.add_widget(close)
        popup.open()
        return popup

    def dialog(self, title, text):
        content = Label(text=text, font_size=dp(13), color=INK, size_hint_y=None, halign="left", valign="top")
        content.bind(texture_size=lambda w, s: setattr(w, "height", max(dp(90), s[1])))
        content.bind(width=lambda w, width: setattr(w, "text_size", (width, None)))
        return self.modal(title, [content], height=430)

    def import_export(self):
        if self.busy or self.file_picker:
            return
        if self.demo_mode:
            self.demo_mode = False
            self.store = self.real_store
            self.mode_label.text = "INSTAGRAM"
        try:
            account = username(self.target.text)
        except DataError:
            self.message("Сначала укажите имя аккаунта.")
            self.target.focus = True
            return
        if platform != "android":
            self.message("Импорт файла доступен в Android-приложении.")
            return

        self.target.disabled = self.settings_btn.disabled = self.connect_btn.disabled = True
        self.sync_btn.disabled = True
        self.message("Выберите ZIP или JSON выгрузки Instagram…")

        def unlock():
            self.file_picker = None
            self.target.disabled = self.settings_btn.disabled = self.connect_btn.disabled = False
            self.sync_btn.disabled = False

        def cancelled():
            unlock()
            self.message("Импорт отменён.")

        def failed(message):
            unlock()
            self.message(message or "Не удалось открыть файл.")

        def selected(path):
            unlock()
            captured_at = datetime.now(timezone.utc).isoformat()

            def work():
                snapshot = load_snapshot(path, account, captured_at, export_complete=True)
                changes = self.store.ingest(snapshot)
                return changes, {sample.kind for sample in snapshot.samples}

            def done(result):
                changes, kinds = result
                self.prefs["target"] = account
                self.save_prefs()
                self.cursor, self.page_stack = None, []
                self.refresh()
                if changes.baselines and not changes.compared:
                    text = "Точка отсчёта сохранена."
                else:
                    summary = self.store.summary(account)
                    text = f"Импортировано. Отписок: {summary['latest_unfollowers']}."
                if "following" not in kinds:
                    text += " В выгрузке нет following.json — невзаимные подписки не обновлены."
                self.message(text)

            self.submit(work, done)

        try:
            from .android_import import pick_instagram_export
            self.file_picker = pick_instagram_export(selected, cancelled, failed)
        except Exception:
            failed("Не удалось открыть выбор файла.")

    def login_dialog(self):
        self.import_export()

    def settings_dialog(self):
        demo = Pill(text="Выйти из демо" if self.demo_mode else "Посмотреть демо", size_hint_y=None, height=dp(43))
        popup = self.modal("Настройки", [demo], height=170)

        def show_demo(_):
            self.demo_mode = not self.demo_mode
            if self.demo_mode:
                self.seed_demo()
            else:
                self.store = self.real_store
            self.mode_label.text = "ДЕМО" if self.demo_mode else "INSTAGRAM"
            self.target.text = "demo_account" if self.demo_mode else self.prefs.get("target", "")
            self.cursor, self.page_stack = None, []
            popup.dismiss()
            self.refresh()

        demo.bind(on_release=show_demo)

    def help_dialog(self):
        self.dialog(
            "Как работает Pulse",
            "Импортируйте ZIP или JSON из выгрузки Instagram. Первый импорт — точка отсчёта. Следующий импорт покажет отписки, новые и невзаимные подписки."
        )

    def on_pause(self):
        self.paused = True
        self.cancel.set()
        return True

    def on_resume(self):
        self.paused = False

    def on_stop(self):
        self.stopping = True
        self.pool.shutdown(wait=False, cancel_futures=True)

