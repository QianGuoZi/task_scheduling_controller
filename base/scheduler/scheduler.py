#接收资源需求，输出部署方案

from typing import Dict


class Scheduler(object):
    def __init__(self, testbed):
        self.testbed = testbed
        self.taskFileList: Dict[int, Dict] = {}

    def resource_schedule(self, taskId : int):
        """
        需要访问Testbed获取目前的资源，还有现有的需求，然后根据调度算法提供
        """
        
        return
    