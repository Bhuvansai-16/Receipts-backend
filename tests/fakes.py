from types import SimpleNamespace


class FakeImage:
    """Stand-in for a Contree image: records run() calls, returns a canned result."""

    def __init__(self, stdout="", exit_code=0):
        self.calls, self.stdout, self.exit_code = [], stdout, exit_code

    async def run(self, shell=None, files=None, **kw):
        self.calls.append({"shell": shell, "files": files, **kw})
        return SimpleNamespace(stdout=self.stdout, stderr="", exit_code=self.exit_code)
