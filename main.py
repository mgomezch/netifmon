import argparse
import atexit
import json
import threading
from itertools import islice
from typing import Optional

import netifaces
from flask import Flask, jsonify
from netaddr.ip import IPNetwork
from prometheus_client import Gauge, generate_latest


def ifirst(predicate, iterator):
  return islice(filter(predicate, iterator), 1)

def the(iterator):
  return (list(islice(iterator, 1)) or [None])[0]


app = Flask(__name__)

# TODO: Replace calls to log() with app.logger.*
def log(*args, **kwargs):
  global app
  app.logger.info(*args, **kwargs)
  print(*args)


class MetricsState(object):
  old: Optional[dict]
  new: Optional[dict]
  diff: dict

  def __init__(self):
    self.old = None
    self.new = None
    self.diff = {}


class Metrics(object):
  polling_interval: int
  interface: str
  prefix_length: int
  file: str
  state: MetricsState
  differs: list['Differ']

  def __init__(
    self,
    polling_interval,
    interface,
    prefix_length,
    file_path,
  ):
    self.polling_interval = polling_interval
    self.interface = interface
    self.prefix_length = prefix_length
    self.file_path = file_path

    self.state = MetricsState()

    self.differs = [
      ipv6_prefix(
        self,
        self.interface,
        self.prefix_length,
      ),
    ]

    if self.file_path:
      log(f"Reading previous persisted state from file {self.file_path}")
      file_data: Optional[dict] = None
      try:
        with open(self.file_path, "r") as file:
          file_data = json.load(file)
        if file_data:
          self.state.old = file_data
          self.state.new = file_data
      except FileNotFoundError:
        log(f"No previous persisted state found in file {self.file_path}")
      except json.JSONDecodeError:
        log(f"Failed to parse persisted state from file {self.file_path}")


  def refresh(self):
    log("Refreshing state")

    new_state = MetricsState()

    new_state.old = self.state.new
    new_state.new = {
      interface: netifaces.ifaddresses(self.interface)
      for interface in netifaces.interfaces()
    }

    new_state.diff = {}
    for differ in self.differs:
      log(f"Running differ {differ.name()}")
      old = differ.get(new_state.old)
      new = differ.get(new_state.new)
      diff = differ.diff(old, new)
      new_state.diff[differ.name()] = diff
      log(f"Ran differ {differ.name()} with old value {old} new value {new} and diff {diff}")  # pylint: disable=line-too-long

    if self.file_path:
      with open(self.file_path, "w+") as file:
        json.dump(new_state.new, file)

    self.state = new_state


class Differ(object):
  metrics: Metrics
  def __init__(self, metrics: Metrics): self.metrics = metrics
  def name(self): return self.__class__.__name__
  def get(self, data): pass
  def diff(self, old, new): pass


class ChangeGaugeDiffer(Differ):
  gauge_name: str
  gauge_description: str
  gauge: Gauge

  def __init__(
    self,
    metrics: Metrics,
    gauge_name: str,
    gauge_description: str,
  ):
    super().__init__(metrics)
    self.gauge_name = gauge_name + '_changed'
    self.gauge_description = gauge_description

    self.gauge = Gauge(
      self.gauge_name,
      self.gauge_description
    )

  # Override this in subclasses if the comparison is not direct, e.g. if get()
  # returns non-primitive data.
  def changed(self, old, new):
    return old != new

  def diff(self, old, new):
    changed = 1 if self.changed(old, new) else 0
    self.gauge.set(changed)
    return changed


class ipv6_prefix(ChangeGaugeDiffer):
  interface: str
  prefix_length: int

  def __init__(
    self,
    metrics: Metrics,
    interface: str,
    prefix_length: int,
  ):
    self.interface = interface
    self.prefix_length = prefix_length
    gauge_name = f'{self.name()}_{interface}_{prefix_length}'
    gauge_description = f"First IPv6 address of the {interface} network interface changed"  # pylint: disable=line-too-long

    super().__init__(
      metrics,
      gauge_name,
      gauge_description,
    )

  # TODO: The delegated prefix should come from routes or somewhere else like
  # DHCP logs, instead of chopping some address.  Assigned addresses need not
  # even be part of the delegated prefix, so this isn't reliable.
  def get(self, interfaces):
    return the(
      IPNetwork(
        f"{addr}/{self.prefix_length}",
      ).network
      for _ in [0] if interfaces is not None
      for interface in [interfaces.get(self.interface)] if interface
      for addresses in [interface.get(netifaces.AF_INET6)] if addresses
      for first_address in islice(addresses, 1)  # TODO: What if there's more?
      for addr in [first_address.get("addr")] if addr
      # TODO: Can we also get the prefix length from first_address?
    )


metrics: Optional[Metrics] = None


@app.route("/interfaces")
def get_interfaces():
  global metrics
  if metrics is None:
    return jsonify({})
  return jsonify(metrics.state.new)

@app.route("/diff")
def get_diff():
  global metrics
  if metrics is None:
    return jsonify({})
  return jsonify(metrics.state.diff)

@app.route("/metrics")
def get_metrics():
  return generate_latest()


# TODO: Why doesn't a threading.thread with an endless loop work?
timer: threading.Timer = threading.Timer(0, lambda _: None, ())

def interrupt():
  global timer
  log("Interrupting metrics refresh timer")
  timer.cancel()

def refresh():
  global metrics
  global timer
  if metrics:
    log("Refreshing metrics")
    metrics.refresh()
  else:
    log("Cannot refresh metrics; metrics object not defined")
  start_timer()

def start_timer():
  global metrics
  global timer

  if metrics:
    log("Starting metrics refresh timer")
    timer = threading.Timer(
      metrics.polling_interval,
      refresh,
      (),
    )
  else:
    log("Cannot start metrics refresh timer; metrics object not defined")

  timer.start()

def start_refresh_loop():
  global timer
  log("Starting metrics refresh loop")
  atexit.register(interrupt)
  start_timer()


def main():
  global app
  global metrics

  parser = argparse.ArgumentParser()

  parser.add_argument(
    "-i",
    "--interface",
    default="eth0",
    help="Interface name",
  )

  parser.add_argument(
    "-p",
    "--prefix-length",
    default=64,
    help="IPv6 network prefix length used when masking the interface's first assigned address to determine its network prefix",  # pylint: disable=line-too-long
  )

  parser.add_argument(
    "-d",
    "--polling-interval",
    default=10,
    help="Polling interval in seconds",
  )

  parser.add_argument(
    "-f",
    "--file",
    default="interface.state",
    help="Persist state in this file",
  )

  args = parser.parse_args()
  if args.interface:
    metrics = Metrics(
      args.polling_interval,
      args.interface,
      args.prefix_length,
      args.file,
    )

    start_refresh_loop()
    app.run()

  else:
    print("No interface name provided")


if __name__ == "__main__":
  main()
