#接收资源需求，输出部署方案

import os
from typing import Dict

from flask import json

# dirName = os.path.abspath (os.path.dirname (__file__))
dirName = '/home/qianguo/controller/'
class Scheduler(object):
    def __init__(self, testbed):
        self.testbed = testbed

    def resource_schedule(self, taskId : int):
        """
        需要访问Testbed获取目前的资源，还有现有的需求，然后根据调度算法提供
        """
        # self.testbed.emulater
        with open(os.path.join(dirName, 'task_links', str(taskId),'links.json'), 'r') as file:
            links_data = json.load(file)
        
        allocation = {}
        for node, connections in links_data.item():
            node_name = str(taskId) + '_' + node
            print(f"Node: {node}")
            for e_name, e_obj in self.testbed.emulator:
                print(f"Emulator name: {e_name}, Emulator object: {e_obj}")
                if e_obj.cpu - e_obj.cpuPreMap > 5 and e_obj.ram - e_obj.ramPreMap > 2:
                    allocation[node_name] = {'emulator': e_name, 'cpu': 5, 'ram': 2}
                else :
                    continue
        return allocation