"""Discovery must not spend one connection timeout on every unused port."""
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import start


class LauncherDiscoveryTests(unittest.TestCase):
    def setUp(self):
        guard = patch('instance_guard.instance_running', return_value=False)
        self.instance_running = guard.start()
        self.addCleanup(guard.stop)

    def test_no_instance_record_does_not_probe_network(self):
        with patch('instance_guard.read_state', return_value=None), patch('start.api') as api:
            self.assertIsNone(start.find_running(48778))
            api.assert_not_called()

    def test_uses_recorded_endpoint_and_checks_identity(self):
        state = dict(host='127.0.0.1', port=54321, pid=123, instance_id='current')
        health = dict(ok=True, service='codex-model-monitor', pid=123, instance_id='current')
        with patch('instance_guard.read_state', return_value=state), patch('start.api', return_value=health) as api:
            self.assertEqual(start.find_running(), ('127.0.0.1', 54321))
            api.assert_called_once_with(54321, '/api/health', timeout=1.0, host='127.0.0.1')

    def test_unreachable_record_does_not_fall_back_to_port_scan(self):
        state = dict(host='localhost', port=48778, pid=123, instance_id='stale')
        with patch('instance_guard.read_state', return_value=state), patch('start.api', side_effect=TimeoutError) as api:
            self.assertIsNone(start.find_running())
            self.assertEqual(api.call_count, 1)

    def test_running_but_unreachable_is_not_reported_as_stopped(self):
        self.instance_running.return_value = True
        state = dict(host='localhost', port=48778, pid=123, instance_id='current')
        with patch('instance_guard.read_state', return_value=state), patch('start.api', side_effect=TimeoutError('probe timeout')):
            with self.assertRaisesRegex(start.DiscoveryError, 'probe timeout'):
                start.find_running()

    def test_running_with_missing_record_is_not_reported_as_stopped(self):
        self.instance_running.return_value = True
        with patch('instance_guard.read_state', return_value=None), patch('start.api') as api:
            with self.assertRaises(start.DiscoveryError):
                start.find_running()
            api.assert_not_called()

    def test_explorer_missing_record_recovers_from_configured_port(self):
        self.instance_running.return_value = True
        health = dict(ok=True, service='codex-model-monitor', backend='cpp',
                      pid=456, instance_id='live-instance')
        with patch('instance_guard.read_state', return_value=None), patch('start.api', return_value=health) as api:
            self.assertEqual(start.find_running(48778), ('localhost', 48778))
            api.assert_called_once_with(48778, '/api/health', timeout=1.0, host='localhost')

    def test_stale_record_from_another_filesystem_view_can_recover(self):
        self.instance_running.return_value = True
        stale = dict(host='localhost', port=48778, pid=123, instance_id='old')
        health = dict(ok=True, service='codex-model-monitor', backend='cpp',
                      pid=456, instance_id='live-instance')
        with patch('instance_guard.read_state', return_value=stale), patch('start.api', return_value=health) as api:
            self.assertEqual(start.find_running(48778), ('localhost', 48778))
            self.assertEqual(api.call_count, 2)
            self.assertTrue(all(call.args[0] == 48778 for call in api.call_args_list))

    def test_unrelated_configured_service_is_never_accepted(self):
        self.instance_running.return_value = True
        for health in ({'ok': True, 'service': 'other'}, [], {}):
            with self.subTest(health=health), patch('instance_guard.read_state', return_value=None), patch('start.api', return_value=health) as api:
                with self.assertRaises(start.DiscoveryError):
                    start.find_running(48778)
                self.assertEqual(api.call_count, 1)

    def test_unrelated_or_malformed_health_does_not_trigger_fallback(self):
        state = dict(host='localhost', port=48778, pid=123, instance_id='current')
        valid = dict(ok=True, service='codex-model-monitor', pid=123, instance_id='current')
        for health in (dict(valid, pid=456), dict(valid, instance_id='other'),
                       dict(valid, service='other'), dict(valid, ok=False), []):
            with self.subTest(health=health), patch('instance_guard.read_state', return_value=state), patch('start.api', return_value=health) as api:
                self.assertIsNone(start.find_running())
                self.assertEqual(api.call_count, 1)


if __name__ == '__main__':
    unittest.main()
