"""Probe: deadlock in the stdio popup reader's shutdown."""
import sys, threading, os, time, json, tempfile
sys.dont_write_bytecode = True
import app.popup_server as ps
print(ps.__file__)
