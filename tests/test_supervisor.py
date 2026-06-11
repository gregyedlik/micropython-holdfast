import unittest

import stub_env

import machine

from holdfast import supervisor

import uasyncio as asyncio


class TestSupervisor(unittest.TestCase):
    def test_crashed_task_triggers_reset(self):
        async def crasher():
            await asyncio.sleep(1)
            raise RuntimeError("boom")

        async def healthy():
            while True:
                await asyncio.sleep(1)

        with self.assertRaises(machine.ResetCalled):
            supervisor.run([("crasher", crasher()), ("healthy", healthy())])

    def test_exited_task_triggers_reset(self):
        async def quitter():
            await asyncio.sleep(1)

        with self.assertRaises(machine.ResetCalled):
            supervisor.run([("quitter", quitter())])


if __name__ == "__main__":
    unittest.main()
