import os

from base import default_testbed
from base.utils import read_json
from gl_manager import GlManager
from base.scheduler.task_analyzer import TaskAnalyzer
from base.scheduler.scheduler import Scheduler

# path of this file.
dirName = os.path.abspath (os.path.dirname (__file__))

# we made up the following physical hardware so this example is NOT runnable.
if __name__ == '__main__':
	testbed = default_testbed (ip='222.201.187.50', dir_name=dirName, manager_class=GlManager, task_analyzer=TaskAnalyzer, scheduler=Scheduler)
	nfsApp = testbed.add_nfs (tag='dml_app', path=os.path.join (dirName, 'dml_app'))
	nfsDataset = testbed.add_nfs (tag='dataset', path=os.path.join (dirName, 'dataset'))
	# 初始化模拟器
	emu1 = testbed.add_emulator ('emulator-1', '222.201.187.51', cpu=128, ram=256, unit='G')
	testbed.__send_emulator_info() # 发送模拟器信息

	
	
	testbed.flask.run(host='0.0.0.0', port=testbed.port, threaded=True)