"""Unit tests for pip_review that do not need network access or pip itself.

Run with ``python -m unittest discover -s tests`` (or with pytest).
"""

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

try:
    from unittest import mock
except ImportError:  # Python 2
    import mock

from pip_review import __main__ as pr

OUTDATED = [
    {'name': 'alpha', 'version': '1.0', 'latest_version': '2.0',
     'latest_filetype': 'wheel'},
    {'name': 'beta-longer', 'version': '0.1.dev1', 'latest_version': '0.2',
     'latest_filetype': 'sdist'},
]


class FilterForwardsTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(pr.filter_forwards([], pr.LIST_ONLY), [])

    def test_keeps_unlisted_options(self):
        args = ['--user', '--timeout', '30']
        self.assertEqual(pr.filter_forwards(args, pr.LIST_ONLY), args)
        self.assertEqual(pr.filter_forwards(args, pr.INSTALL_ONLY), args)

    def test_drops_excluded_option_and_its_value(self):
        args = ['--exclude', 'foo', '--user']
        self.assertEqual(pr.filter_forwards(args, pr.LIST_ONLY), ['--user'])

    def test_drops_short_excluded_option(self):
        self.assertEqual(pr.filter_forwards(['-U'], pr.INSTALL_ONLY), [])
        self.assertEqual(pr.filter_forwards(['-U'], pr.LIST_ONLY), ['-U'])

    def test_drops_value_of_excluded_option_only(self):
        args = ['-c', 'constraints.txt', '--pre', '--timeout', '5']
        self.assertEqual(
            pr.filter_forwards(args, pr.INSTALL_ONLY),
            ['--pre', '--timeout', '5'],
        )

    def test_leading_positional_is_dropped(self):
        self.assertEqual(pr.filter_forwards(['stray'], pr.LIST_ONLY), [])

    def test_sets_do_not_overlap(self):
        self.assertEqual(pr.LIST_ONLY & pr.INSTALL_ONLY, set())


class ParseLegacyTest(unittest.TestCase):
    def test_parses_current_and_latest(self):
        out = 'requests (Current: 2.0 Latest: 2.1 [wheel])'
        self.assertEqual(pr.parse_legacy(out), [
            {'name': 'requests', 'version': '2.0', 'latest_version': '2.1'},
        ])

    def test_skips_lines_without_two_versions(self):
        out = 'a header\nfoo (Current: 1.0)\nbar-baz (Current: 1.0 Latest: 1.1)'
        self.assertEqual([p['name'] for p in pr.parse_legacy(out)],
                         ['bar-baz'])

    def test_empty_output(self):
        self.assertEqual(pr.parse_legacy(''), [])


class TableTest(unittest.TestCase):
    def test_extract_column(self):
        self.assertEqual(
            pr.extract_column(OUTDATED, 'name', 'Package'),
            ['Package', 'alpha', 'beta-longer'],
        )

    def test_extract_table_has_one_column_per_heading(self):
        table = pr.extract_table(OUTDATED)
        self.assertEqual(len(table), len(pr.COLUMNS))
        self.assertEqual(sorted(col[0] for col in table),
                         sorted(pr.COLUMNS))

    def test_column_width_ignores_empty_cells(self):
        self.assertEqual(pr.column_width(['ab', '', 'abcd']), 4)

    def test_format_table(self):
        columns = [['Package', 'alpha'], ['Version', '1.0']]
        self.assertEqual(
            pr.format_table(columns),
            '\n'.join([
                'Package Version',
                '---------------',
                'alpha   1.0    ',
                '---------------',
            ]),
        )


class InteractiveAskerTest(unittest.TestCase):
    def ask(self, answers, times=1):
        asker = pr.InteractiveAsker()
        with mock.patch.object(pr, 'input', side_effect=answers,
                               create=True) as fake:
            results = [asker.ask('Go?') for _ in range(times)]
        return results, fake

    def test_valid_answer(self):
        results, _ = self.ask([' Y '])
        self.assertEqual(results, ['y'])

    def test_reprompts_on_invalid_answer(self):
        results, fake = self.ask(['what', 'n'])
        self.assertEqual(results, ['n'])
        self.assertEqual(fake.call_count, 2)

    def test_empty_answer_repeats_last_answer(self):
        results, fake = self.ask(['n', ''], times=2)
        self.assertEqual(results, ['n', 'n'])
        self.assertIn('(n)', fake.call_args[0][0])

    def test_empty_first_answer_asks_again(self):
        results, fake = self.ask(['', 'y'])
        self.assertEqual(results, ['y'])
        self.assertEqual(fake.call_count, 2)

    def test_all_is_cached(self):
        results, fake = self.ask(['a'], times=3)
        self.assertEqual(results, ['a', 'a', 'a'])
        self.assertEqual(fake.call_count, 1)

    def test_quit_is_cached(self):
        results, fake = self.ask(['q'], times=2)
        self.assertEqual(results, ['q', 'q'])
        self.assertEqual(fake.call_count, 1)

    def test_yes_and_no_are_not_cached(self):
        results, fake = self.ask(['y', 'n'], times=2)
        self.assertEqual(results, ['y', 'n'])
        self.assertEqual(fake.call_count, 2)


class ConfirmTest(unittest.TestCase):
    def test_confirm(self):
        with mock.patch.object(pr, 'input', side_effect=['maybe', 'Y'],
                               create=True):
            self.assertTrue(pr.confirm('Sure? '))
        with mock.patch.object(pr, 'input', side_effect=['n'], create=True):
            self.assertFalse(pr.confirm('Sure? '))


class CheckOutputTest(unittest.TestCase):
    def test_returns_output(self):
        out = pr.check_output([sys.executable, '-c', 'print("hi")'])
        self.assertEqual(out.strip(), b'hi')

    def test_raises_with_output_on_failure(self):
        cmd = [sys.executable, '-c',
               'import sys; print("oops"); sys.exit(3)']
        with self.assertRaises(subprocess.CalledProcessError) as ctx:
            pr.check_output(cmd)
        self.assertEqual(ctx.exception.returncode, 3)
        self.assertEqual(ctx.exception.output.strip(), b'oops')


class GetOutdatedPackagesTest(unittest.TestCase):
    def run_with(self, pip_version, output):
        with mock.patch.object(pr.pip, '__version__', pip_version), \
                mock.patch.object(pr, 'check_output',
                                  return_value=output) as fake:
            packages = pr.get_outdated_packages(['--user'])
        return packages, fake.call_args[0][0]

    def test_modern_pip_uses_json(self):
        packages, command = self.run_with(
            '24.0', b'[{"name": "a", "version": "1", "latest_version": "2"}]')
        self.assertEqual(packages[0]['name'], 'a')
        self.assertEqual(command[:len(pr.PIP_CMD) + 2],
                         pr.PIP_CMD + ['list', '--outdated'])
        self.assertIn('--user', command)
        self.assertIn('--format=json', command)
        self.assertIn('--disable-pip-version-check', command)

    def test_legacy_pip_parses_text(self):
        packages, command = self.run_with(
            '8.1.2', b'foo (Current: 1.0 Latest: 1.1)\n')
        self.assertEqual(packages, [
            {'name': 'foo', 'version': '1.0', 'latest_version': '1.1'},
        ])
        self.assertIn('--disable-pip-version-check', command)
        self.assertNotIn('--format=json', command)

    def test_pip_9_exactly_is_still_legacy(self):
        _, command = self.run_with('9.0', b'')
        self.assertNotIn('--format=json', command)

    def test_very_old_pip_has_no_version_check_flag(self):
        _, command = self.run_with('1.5.6', b'')
        self.assertNotIn('--disable-pip-version-check', command)


class UpdatePackagesTest(unittest.TestCase):
    PACKAGES = [{'name': 'a', 'version': '1'}, {'name': 'b', 'version': '2'}]

    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp)

    def patch_call(self):
        # update_packages reuses and mutates one list, so record copies
        patcher = mock.patch.object(pr.subprocess, 'call')
        fake = patcher.start()
        self.addCleanup(patcher.stop)
        fake.recorded = []
        fake.side_effect = lambda cmd, **kw: fake.recorded.append(
            cmd[len(pr.PIP_CMD):])
        return fake

    def test_single_install_of_all_packages(self):
        fake = self.patch_call()
        pr.update_packages(self.PACKAGES, ['--user'], False, False)
        self.assertEqual(fake.recorded,
                         [['install', '-U', '--user', 'a', 'b']])

    def test_continue_on_fail_installs_one_by_one(self):
        fake = self.patch_call()
        pr.update_packages(self.PACKAGES, ['--user'], True, False)
        self.assertEqual(fake.recorded, [
            ['install', '-U', '--user', 'a'],
            ['install', '-U', '--user', 'b'],
        ])

    def test_freeze_writes_current_versions(self):
        self.patch_call()
        pr.update_packages(self.PACKAGES, [], False, True)
        with open('requirements.txt') as f:
            self.assertEqual(f.read(), 'a==1\nb==2\n')

    def test_no_freeze_writes_nothing(self):
        self.patch_call()
        pr.update_packages(self.PACKAGES, [], False, False)
        self.assertFalse(os.path.exists('requirements.txt'))


class LoggingTest(unittest.TestCase):
    def tearDown(self):
        logger = logging.getLogger('pip-review')
        for handler in list(logger.handlers):
            logger.removeHandler(handler)

    def test_levels(self):
        self.assertEqual(pr.setup_logging(False).level, logging.INFO)
        self.assertEqual(pr.setup_logging(True).level, logging.DEBUG)

    def test_stdout_filter(self):
        flt = pr.StdOutFilter()

        def record(level):
            return logging.LogRecord('x', level, __file__, 1, 'm', (), None)

        self.assertTrue(flt.filter(record(logging.DEBUG)))
        self.assertTrue(flt.filter(record(logging.INFO)))
        self.assertFalse(flt.filter(record(logging.WARNING)))
        self.assertFalse(flt.filter(record(logging.ERROR)))


class ParseArgsTest(unittest.TestCase):
    def parse(self, *argv):
        with mock.patch.object(sys, 'argv', ['pip-review'] + list(argv)):
            return pr.parse_args()

    def test_defaults(self):
        args, forwarded = self.parse()
        self.assertFalse(any(vars(args).values()))
        self.assertEqual(forwarded, [])

    def test_flags(self):
        args, _ = self.parse('-v', '-r', '-i', '-a', '-C', '-p', '-P',
                             '--freeze-outdated-packages')
        self.assertTrue(all(vars(args).values()))

    def test_unknown_options_are_forwarded(self):
        args, forwarded = self.parse('--user', '--auto', '--timeout', '30')
        self.assertTrue(args.auto)
        self.assertEqual(forwarded, ['--user', '--timeout', '30'])


class MainTest(unittest.TestCase):
    def run_main(self, *argv, **kwargs):
        outdated = kwargs.pop('outdated', OUTDATED)
        answers = kwargs.pop('answers', [])
        logger = mock.Mock()
        with mock.patch.object(sys, 'argv', ['pip-review'] + list(argv)), \
                mock.patch.object(pr, 'setup_logging', return_value=logger), \
                mock.patch.object(pr, 'get_outdated_packages',
                                  return_value=outdated) as get, \
                mock.patch.object(pr, 'update_packages') as update, \
                mock.patch.object(pr, 'ask_to_install',
                                  side_effect=answers):
            pr.main()
        lines = [call[0][0] for call in logger.info.call_args_list]
        return lines, get, update

    def test_up_to_date(self):
        lines, _, update = self.run_main(outdated=[])
        self.assertEqual(lines, ['Everything up-to-date'])
        update.assert_not_called()

    def test_default_lists_updates_without_installing(self):
        lines, _, update = self.run_main()
        self.assertEqual(lines, [
            'alpha==2.0 is available (you have 1.0)',
            'beta-longer==0.2 is available (you have 0.1.dev1)',
        ])
        update.assert_not_called()

    def test_raw(self):
        lines, _, update = self.run_main('--raw')
        self.assertEqual(lines, ['alpha==2.0', 'beta-longer==0.2'])
        update.assert_not_called()

    def test_raw_with_nothing_outdated_prints_nothing(self):
        lines, _, _ = self.run_main('--raw', outdated=[])
        self.assertEqual(lines, [])

    def test_raw_and_interactive_conflict(self):
        with self.assertRaises(SystemExit):
            self.run_main('--raw', '--interactive')

    def test_preview_shows_table_then_lists(self):
        lines, _, _ = self.run_main('--preview')
        self.assertIn('alpha', lines[0])
        self.assertIn('Package', lines[0])
        self.assertEqual(len(lines), 3)

    def test_preview_only_stops_after_table(self):
        lines, _, update = self.run_main('--preview-only', '--auto')
        self.assertEqual(len(lines), 1)
        update.assert_not_called()

    def test_auto_installs_everything(self):
        _, _, update = self.run_main('--auto')
        update.assert_called_once_with(OUTDATED, [], False, False)

    def test_auto_passes_options_through(self):
        _, get, update = self.run_main(
            '--auto', '--continue-on-fail', '--freeze-outdated-packages',
            '--user', '--force-reinstall', '--not-required')
        # install-only options do not reach `pip list`, list-only ones do not
        # reach `pip install`
        get.assert_called_once_with(['--user', '--not-required'])
        update.assert_called_once_with(
            OUTDATED, ['--user', '--force-reinstall'], True, True)

    def test_interactive_installs_selected_only(self):
        _, _, update = self.run_main('--interactive', answers=['y', 'n'])
        update.assert_called_once_with(OUTDATED[:1], [], False, False)

    def test_interactive_answer_all(self):
        _, _, update = self.run_main('-i', answers=['a', 'a'])
        update.assert_called_once_with(OUTDATED, [], False, False)

    def test_interactive_nothing_selected(self):
        _, _, update = self.run_main('-i', answers=['n', 'q'])
        update.assert_not_called()


if __name__ == '__main__':
    unittest.main()
