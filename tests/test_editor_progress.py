"""Editor progress, concurrent completion, explicit reset and workspace choices."""
import gc
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vtgui.core import DEFAULTS, Engine, Store
from vtgui.ui import App, ScheduleDialog


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='VTGUI editor 中文 ')
        self.base = Path(self.temp.name)
        self.roots = [self.base / f'project {i}' for i in range(3)]
        for root in self.roots:
            root.mkdir()
        self.store = Store(self.base / 'data')
        self.store.save_settings({'roots': list(map(str, self.roots)), 'root': str(self.roots[0])})
        self.engine = Engine(self.store)
        self.app = App(self.store, self.engine)
        self.app.update()
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            if dialog.winfo_exists():
                dialog.destroy()
        self.engine.close()
        # Each test owns a Tk root; cancel its timers before the next root starts.
        for timer in self.app.tk.call('after', 'info'):
            self.app.after_cancel(timer)
        self.app.destroy()
        self.dialogs.clear()
        self.app = None
        # Dispose Tk cycles on the main thread before later engine tests start.
        gc.collect()
        self.store.db.close()
        self.temp.cleanup()

    def schedule(self, **values):
        ident = self.store.save_schedule({'job': {**DEFAULTS, 'root': str(self.roots[0]), 'command': 'echo S01E{episode}'},
                                         'time': '09:00', 'days': [0], 'episode_start': '03', 'episode_total': 4, 'episodes_per_trigger': 1, **values})
        return next(s for s in self.store.schedules() if s['id'] == ident)

    def dialog(self, spec):
        dialog = ScheduleDialog(self.app, spec)
        self.dialogs.append(dialog)
        return dialog

    def complete(self, spec, status='completed'):
        ident = self.store.trigger_schedule(spec['id'])
        self.store.finish_run(ident, status, 0 if status == 'completed' else 1, 'fixture')
        return ident

    def refresh_progress(self, editor):
        if editor.progress_after:
            editor.after_cancel(editor.progress_after)
            editor.progress_after = None
        editor.refresh_episode_progress()

    def test_task_directory_choices_are_independent_and_saved(self):
        self.app.work_root.set(str(self.roots[2]))
        self.app.command.insert('1.0', 'echo test')
        job = self.app.current_job()
        self.assertEqual(job['root'], str(self.roots[2]))
        spec = self.schedule()
        editor = self.dialog(spec)
        self.assertEqual(tuple(editor.root_combo['values']), tuple(map(str, self.roots)))
        editor.root_dir.set(str(self.roots[1]))
        editor.save()
        self.assertEqual(self.store.schedules()[0]['job']['root'], str(self.roots[1]))
        self.assertEqual(self.app.work_root.get(), str(self.roots[2]))
        self.assertEqual(self.store.settings()['root'], str(self.roots[0]))

    def test_extra_times_add_limit_remove_save_and_reopen(self):
        spec = self.schedule()
        editor = self.dialog(spec)
        self.assertEqual(editor.extra_times, [])
        for when in ('23:00', '00:00', '12:30'):
            editor.add_time_button.invoke()
            editor.extra_times[-1].set(when)
        self.assertEqual(editor.add_time_button.cget('state'), 'disabled')
        editor.add_extra_time()
        self.assertEqual(len(editor.extra_times), 3)
        editor.geometry('790x610')
        editor.update()
        for field in editor.extra_time_entries:
            self.assertLessEqual(field.winfo_rootx() + field.winfo_width(), editor.winfo_rootx() + editor.winfo_width())
        editor.save()
        saved = self.store.schedules()[0]
        self.assertEqual(saved['extra_times'], ['23:00', '00:00', '12:30'])
        self.assertEqual(self.app.schedules_tree.item(spec['id'], 'values')[2], '00:00 / 09:00 / 12:30 / 23:00')
        editor = self.dialog(saved)
        self.assertEqual([v.get() for v in editor.extra_times], saved['extra_times'])
        editor.remove_extra_time(editor.extra_times[1])
        self.assertEqual(editor.add_time_button.cget('state'), 'normal')
        editor.add_time_button.invoke()
        editor.extra_times[-1].set('06:00')
        editor.save()
        self.assertEqual(self.store.schedules()[0]['extra_times'], ['23:00', '12:30', '06:00'])
        editor = self.dialog(self.store.schedules()[0])
        for variable in list(editor.extra_times):
            editor.remove_extra_time(variable)
        editor.save()
        self.assertEqual(self.store.schedules()[0]['extra_times'], [])

    def test_invalid_extra_time_keeps_editor_open_and_schedule_unchanged(self):
        spec = self.schedule()
        editor = self.dialog(spec)
        editor.add_extra_time()
        for when in ('', '25:00', '9:0'):
            editor.extra_times[0].set(when)
            with self.subTest(when=when), patch('vtgui.ui.messagebox.showerror') as error:
                editor.save()
                error.assert_called_once()
                self.assertTrue(editor.winfo_exists())
                self.assertEqual(self.store.schedules()[0]['extra_times'], [])

    def test_open_and_save_display_next_episode_without_reset(self):
        spec = self.schedule()
        self.complete(spec)
        editor = self.dialog(spec)
        self.assertEqual(editor.episode_vars['episode_start'].get(), '04')
        editor.when.set('10:00')
        editor.save()
        saved = self.store.schedules()[0]
        self.assertEqual(saved['episode_start'], '03')
        self.assertEqual(self.store.episode_progress(saved), 4)
        self.assertEqual(self.store.run(self.store.trigger_schedule(spec['id']))['episode'], 4)

    def test_editor_tracks_completion_and_failure_without_skipping(self):
        spec = self.schedule(episode_total=8)
        editor = self.dialog(spec)
        self.complete(spec)
        self.refresh_progress(editor)
        self.assertEqual(editor.episode_vars['episode_start'].get(), '04')
        self.complete(spec, 'failed')
        self.refresh_progress(editor)
        self.assertEqual(editor.episode_vars['episode_start'].get(), '04')
        self.complete(spec, 'cancelled')
        self.refresh_progress(editor)
        self.assertEqual(editor.episode_vars['episode_start'].get(), '04')
        editor.save()
        self.assertEqual(self.store.episode_progress(spec), 4)

    def test_manual_edit_is_not_overwritten_and_resets_when_saved(self):
        spec = self.schedule(episode_total=8)
        editor = self.dialog(spec)
        editor.episode_vars['episode_start'].set('06')
        self.complete(spec)
        self.refresh_progress(editor)
        self.assertEqual(editor.episode_vars['episode_start'].get(), '06')
        editor.save()
        self.assertEqual(self.store.episode_progress(spec), 6)

    def test_manual_reset_to_original_start_takes_effect(self):
        spec = self.schedule(episode_total=8)
        self.complete(spec)
        editor = self.dialog(spec)
        self.assertEqual(editor.episode_vars['episode_start'].get(), '04')
        editor.episode_vars['episode_start'].set('03')
        editor.save()
        self.assertEqual(self.store.episode_progress(spec), 3)
        self.assertEqual(self.store.run(self.store.trigger_schedule(spec['id']))['episode'], 3)

    def test_manual_reset_to_original_start_is_blocked_during_active_batch(self):
        spec = self.schedule(episode_total=8, episodes_per_trigger=2)
        self.complete(spec)
        editor = self.dialog(spec)
        editor.episode_vars['episode_start'].set('03')
        with patch('vtgui.ui.messagebox.showerror') as error:
            editor.save()
            error.assert_called_once()
        self.assertEqual(self.store.episode_progress(spec), 4)
        self.assertTrue(editor.winfo_exists())

    def test_completed_schedule_can_save_and_extend_without_replay(self):
        spec = self.schedule(episode_total=3)
        self.complete(spec)
        editor = self.dialog(spec)
        self.assertEqual(editor.episode_vars['episode_start'].get(), '04')
        self.assertIn('全部', editor.progress_text.get())
        editor.save()
        self.assertIsNone(self.store.trigger_schedule(spec['id']))
        editor = self.dialog(self.store.schedules()[0])
        editor.episode_vars['episode_total'].set('5')
        editor.save()
        self.assertEqual(self.store.run(self.store.trigger_schedule(spec['id']))['episode'], 4)

    def test_progress_display_save_while_batch_active_does_not_reset(self):
        spec = self.schedule(episode_total=8, episodes_per_trigger=2)
        self.complete(spec)
        editor = self.dialog(spec)
        self.assertEqual(editor.episode_vars['episode_start'].get(), '04')
        with patch('vtgui.ui.messagebox.showerror') as error:
            editor.save()
            error.assert_not_called()
        self.assertEqual(self.store.episode_progress(spec), 4)
        self.assertEqual(self.store.schedules()[0]['episode_start'], '03')

    def test_settings_remove_directory_updates_choices_without_rewriting_task(self):
        spec = self.schedule()
        for variable in self.app.root_vars:
            variable.set('')
        self.app.root_vars[0].set(str(self.roots[1]))
        self.app.save_settings()
        self.assertEqual(tuple(self.app.work_root_combo['values']), (str(self.roots[1]),))
        self.assertEqual(self.store.schedules()[0]['job']['root'], str(self.roots[0]))
        editor = self.dialog(spec)
        self.assertEqual(editor.root_dir.get(), str(self.roots[0]))
        self.assertEqual(tuple(editor.root_combo['values']), (str(self.roots[1]),))


if __name__ == '__main__':
    unittest.main()
