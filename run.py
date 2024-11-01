import argparse
import time

from bomoto.config import get_cfg
from bomoto.engine import Engine

parser = argparse.ArgumentParser()
parser.add_argument("--cfg", type=str, required=True, help="Path to config file")
args = parser.parse_args()

cfg = get_cfg(args.cfg)
engine = Engine(cfg)

start_time = time.time()

engine.run()

print("Total time:", time.time() - start_time)
