"""Fixture policy that hangs and leaves a child process behind: the host must kill the whole process tree."""
import os
import subprocess
import time


def initialize(parameters):
    child = subprocess.Popen(["sleep", "120"])
    with open(parameters["pidfile"], "w") as f:
        f.write(str(child.pid))
    return {}


def on_step(ctx, state):
    time.sleep(30)
    return {"intents": [], "state": state}
