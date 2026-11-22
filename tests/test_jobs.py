"""Job control: needs a real controlling terminal, so these run on a pty."""
import os, re, signal, time, unittest
from term import Session

CTRL_Z, CTRL_C, ENTER = "\x1a", "\x03", "\r"


class JobCase(unittest.TestCase):
    def setUp(self):
        self.s = Session(rows=30, cols=80)
        self.addCleanup(self.s.close)

    def cmd(self, c, quiet=0.4): return self.s.send(c + ENTER, quiet)
    def text(self): return self.s.text()

    def children(self):
        """pids whose parent is the shell"""
        out = []
        for p in os.listdir("/proc"):
            if p.isdigit():
                try:
                    with open(f"/proc/{p}/stat") as f: st = f.read()
                except OSError: continue
                fields = st[st.rindex(")") + 2:].split()
                if int(fields[1]) == self.s.pid: out.append((int(p), fields[0]))
        return out


class Foreground(JobCase):
    def test_ctrl_z_stops_and_returns_to_prompt(self):
        self.cmd("sleep 30")
        self.s.send(CTRL_Z, 0.8)
        self.assertIn("Stopped", self.text())
        self.assertIn("sleep 30", self.text().split("Stopped")[1])
        self.assertIn("[148]>", self.text())                      # exit status 128+SIGTSTP
        self.assertEqual([st for _, st in self.children()], ["T"])

    def test_shell_still_usable_after_stop(self):
        self.cmd("sleep 30"); self.s.send(CTRL_Z, 0.8)
        self.assertIn("\nalive", self.cmd("echo alive"))

    def test_jobs_lists_stopped(self):
        self.cmd("sleep 30"); self.s.send(CTRL_Z, 0.8)
        self.assertRegex(self.cmd("jobs"), r"\[1\]\+\s+Stopped\s+sleep 30")

    def test_fg_resumes_and_ctrl_c_kills(self):
        self.cmd("sleep 30"); self.s.send(CTRL_Z, 0.8)
        self.cmd("fg", 0.5)
        self.assertEqual([st for _, st in self.children()], ["S"])
        self.s.send(CTRL_C, 0.8)
        self.assertIn("[130]>", self.text())
        self.assertEqual(self.children(), [])
        after_jobs = self.cmd("jobs").rsplit("jobs", 1)[1]
        self.assertNotRegex(after_jobs, r"Running|Stopped")        # nothing left in the job table

    def test_fg_completion_status(self):
        self.cmd("sh -c 'sleep 0.6; exit 5'"); self.s.send(CTRL_Z, 0.3)
        self.cmd("fg", 1.5)
        self.assertIn("[5]>", self.text())

    def test_ctrl_c_kills_foreground_job_not_shell(self):
        self.cmd("sleep 30"); self.s.send(CTRL_C, 0.8)
        self.assertTrue(self.s.alive())
        self.assertIn("[130]>", self.text())

    def test_pipeline_stops_and_resumes_as_one(self):
        self.cmd("sleep 30 | cat"); self.s.send(CTRL_Z, 1.0)
        self.assertIn("Stopped", self.text())
        self.assertEqual({st for _, st in self.children()}, {"T"})
        self.assertEqual(len(self.children()), 2)
        self.cmd("fg", 0.5)
        self.assertEqual({st for _, st in self.children()}, {"S"})
        self.s.send(CTRL_C, 0.8)
        self.assertEqual(self.children(), [])

    def test_interactive_program_gets_the_terminal(self):
        self.cmd("cat"); self.s.send("typed\r", 0.4)
        self.assertGreaterEqual(self.text().count("typed"), 2)     # tty echo + cat's output
        self.s.send(CTRL_C, 0.5)

    def test_terminal_modes_restored_after_job(self):
        self.cmd("stty -echo; sleep 0.2", 0.6)                     # job leaves the tty in a changed mode
        self.cmd("stty -a | head -c 0")
        self.s.send("visible", 0.3)
        self.assertIn("visible", self.text().split("\n")[-1])


class Background(JobCase):
    def test_ampersand_reports_job(self):
        self.assertRegex(self.cmd("sleep 30 &"), r"\[1\] \d+")
        self.assertRegex(self.cmd("jobs"), r"\[1\]\+\s+Running\s+sleep 30 &")

    def test_shell_continues_while_running(self):
        self.cmd("sleep 30 &")
        self.assertIn("\nok", self.cmd("echo ok"))

    def test_done_notification_before_next_prompt(self):
        self.cmd("sleep 0.3 &")
        time.sleep(0.8)
        self.assertRegex(self.cmd(""), r"\[1\]\s+Done\s+sleep 0.3")

    def test_failed_job_reports_exit_status(self):
        self.cmd("sh -c 'exit 3' &")
        time.sleep(0.5)
        self.assertRegex(self.cmd(""), r"\[1\]\s+Exit 3")

    def test_bg_resumes_stopped_job(self):
        self.cmd("sleep 30"); self.s.send(CTRL_Z, 0.8)
        self.cmd("bg")
        self.assertRegex(self.cmd("jobs"), r"Running\s+sleep 30")
        self.assertEqual([st for _, st in self.children()], ["S"])

    def test_fg_with_job_number(self):
        self.cmd("sleep 30 &"); self.cmd("sleep 31 &")
        out = self.cmd("fg %1")
        self.assertIn("sleep 30", out.split("fg %1")[1])
        self.s.send(CTRL_C, 0.8)
        self.assertRegex(self.cmd("jobs"), r"\[2\].*sleep 31")

    def test_job_numbers_are_reused(self):
        self.cmd("sleep 0.2 &"); time.sleep(0.6); self.cmd("")
        self.assertRegex(self.cmd("sleep 30 &"), r"\[1\] \d+")

    def test_kill_job(self):
        self.cmd("sleep 30 &")
        self.cmd("kill %1")
        time.sleep(0.3)
        self.assertRegex(self.cmd(""), r"\[1\]\s+Exit 143")
        self.assertEqual(self.children(), [])

    def test_kill_stopped_job(self):
        self.cmd("sleep 30"); self.s.send(CTRL_Z, 0.8)
        self.cmd("kill %1"); time.sleep(0.4)
        self.cmd("")
        self.assertEqual(self.children(), [])

    def test_wait_for_background_job(self):
        self.cmd("sh -c 'sleep 0.5; exit 4' &")
        self.cmd("wait", 1.5)
        self.assertIn("[4]>", self.text())

    def test_background_job_not_killed_by_ctrl_c(self):
        self.cmd("sleep 30 &")
        self.s.send(CTRL_C, 0.4)
        self.assertEqual([st for _, st in self.children()], ["S"])

    def test_background_reader_is_stopped_not_hung(self):
        self.cmd("cat &"); time.sleep(0.5)
        self.assertRegex(self.cmd(""), r"Stopped\s+cat")            # SIGTTIN
        self.assertTrue(self.s.alive())


class Exiting(JobCase):
    def test_exit_warns_about_stopped_jobs_then_leaves(self):
        self.cmd("sleep 30"); self.s.send(CTRL_Z, 0.8)
        self.cmd("exit")
        self.assertIn("stopped jobs", self.text())
        self.assertTrue(self.s.alive())
        self.cmd("exit", 0.3)
        self.assertTrue(self.s.wait_exit())

    def test_ctrl_d_warns_too(self):
        self.cmd("sleep 30"); self.s.send(CTRL_Z, 0.8)
        self.s.send("\x04", 0.5)
        self.assertIn("stopped jobs", self.text())
        self.assertTrue(self.s.alive())
        self.s.send("\x04", 0.3)
        self.assertTrue(self.s.wait_exit())

    def test_leaving_hangs_up_jobs(self):
        self.cmd("sleep 30 &")
        before = [p for p, _ in self.children()]
        self.cmd("exit", 0.8)
        time.sleep(0.3)
        for p in before: self.assertFalse(os.path.exists(f"/proc/{p}"), f"{p} survived")


class NonInteractive(unittest.TestCase):
    def test_background_in_script_runs_with_devnull_stdin(self):
        from helpers import run
        r = run("cat &\nwait\necho done")
        self.assertIn("done", r.out)

    def test_wait_returns_status_in_script(self):
        from helpers import run
        r = run("sh -c 'exit 6' &\nwait\necho $?")
        self.assertEqual(r.out.strip(), "6")

    def test_kill_builtin_signal_list(self):
        from helpers import run
        self.assertIn("TERM", run("kill -l").out)


if __name__ == "__main__":
    unittest.main()
