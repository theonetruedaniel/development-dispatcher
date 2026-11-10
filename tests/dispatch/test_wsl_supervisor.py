import unittest
from scripts.dispatch.wsl_supervisor import validate_request, sandbox_argv


class WslSupervisor(unittest.TestCase):
    def request(self):
        return dict(root="/mnt/c/scratch/worker", argv=["/usr/bin/python3", "-c", "print('ok')"],
                    timeout_seconds=5, output_limit=65536)

    def test_profile_is_fixed_and_does_not_export_host(self):
        req = validate_request(self.request())
        args = sandbox_argv(req)
        self.assertIn("--unshare-all", args)
        self.assertIn("--clearenv", args)
        self.assertIn("--die-with-parent", args)
        self.assertEqual(args[args.index("--bind")+1:args.index("--bind")+3],
                         ["/mnt/c/scratch/worker", "/work"])
        self.assertNotIn("/home", args)
        self.assertNotIn("/mnt/c", args)
        mounts = [(args[i+1], args[i+2]) for i, arg in enumerate(args) if arg == '--ro-bind']
        self.assertFalse(any(target in ('/usr', '/lib', '/lib64') for _, target in mounts))
        self.assertIn('/usr/bin/python3.14', [target for _, target in mounts])

    def test_rejects_unbounded_or_malformed_requests(self):
        for update in ({"root":"/"}, {"root":"/mnt/c"}, {"root":"/mnt/c/"},
                       {"root":"/mnt/c/a/../b"}, {"timeout_seconds":0},
                       {"timeout_seconds":1801}, {"timeout_seconds":True},
                       {"output_limit":4194305}, {"argv":"sh -c anything"},
                       {"argv":[]}, {"network":True}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate_request(dict(self.request(), **update))
