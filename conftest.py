"""让测试无需安装即可 import src/ 下的包。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
