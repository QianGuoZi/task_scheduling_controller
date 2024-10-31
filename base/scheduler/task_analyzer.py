# 接收用户请求（包括数据集、dml代码、manager.py、节点信息、网络拓扑）
# 分析用户请求，将资源请求的内容丢给scheduler，从scheduler中拿到部署方案
# 将方案丢给edgetb部署

import os
import shutil
from typing import Dict
import zipfile
from flask import request

# dirName = os.path.abspath (os.path.dirname (__file__))
dirName = '/home/qianguo/controller/'

class TaskAnalyzer(object):
    def __init__(self, testbed):
        self.testbed = testbed
        self.taskFileList: Dict[int, Dict] = {} 
        self.currtkId: int = 0

        self.__load_default_route()
        
    def __load_default_route(self):
        @self.testbed.flask.route('/taskRequestFile', methods=['POST'])
        def route_receive_request():
            """
            接收用户发送的任务文件，接收压缩包（包括links.json文件，dml_app,dml_tool,manager.py文件）
            task_file
                ├─ links.json
                ├─ manager.py
                ├─ dataset*
                    ├─ test_data
                    ├─ train_data
                ├─ dml_tool
                    ├─ dataset.json
                    ├─ structure.json
                    ├─ structure_conf.py
                    ├─ dataset_conf.py
                ├─ dml_app
                    ├─ nns
                    ├─ dml_req.txt
                    ├─ peer.py
                    ├─ Dockerfile
            """
            def analyse_file(taskId: int):
                """
                处理用户的文件，将压缩包解压并放到不同的文件夹中
                dml_app和dml_file需要和worker挂载，要不直接挂载一个大的文件夹，然后往里面更新算了（
                那挂载可以不动了，直接在下面为task创建对应的文件夹，文件路径改改
                worker_utils可以获得taskId，喜
                """
                zip_filename = f"{taskId}_taskFile.zip"
                zip_path = os.path.join(dirName, "task_file", zip_filename)
                
                current_directory = os.path.join(dirName, "task_file", str(taskId))
                os.makedirs(current_directory, exist_ok=True)
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(current_directory)
                            
                # os.remove(zip_path)

                # 复制 links.json到task_links/{taskId}/links.json
                links_json_path = os.path.join(current_directory, 'links.json')
                target_links_json_path = os.path.join(dirName, "task_links", str(taskId))
                if not os.path.exists(target_links_json_path) :
                    os.makedirs(target_links_json_path)
                shutil.copy2(links_json_path, target_links_json_path)

                # 复制 manager.py到task_manager/{taskId}/ml_manager.py
                manager_path = os.path.join(current_directory, 'ml_manager.py')
                target_manager_path = os.path.join(dirName, "task_manager", str(taskId))
                if not os.path.exists(target_manager_path) :
                    os.makedirs(target_manager_path)
                shutil.copy2(manager_path, target_manager_path)
                
                # 创建 dml_tool 子目录
                target_dml_file_dir = os.path.join(dirName, "dml_tool", str(taskId))
                os.makedirs(target_dml_file_dir, exist_ok=True)
                
                # 复制 dml_file 文件夹到指定目录 /controller/dml_file/{taskId}/
                source_dml_file_dir = os.path.join(current_directory, 'dml_tool')
                if os.path.exists(source_dml_file_dir):
                    shutil.copytree(source_dml_file_dir, target_dml_file_dir, dirs_exist_ok=True)

                # 创建 dml_app 子目录
                target_dml_app_dir = os.path.join(dirName, "dml_app", str(taskId))
                os.makedirs(target_dml_app_dir, exist_ok=True)
                
                # 移动 dml_app 文件夹到指定目录 /controller/dml_app/{taskId}/
                source_dml_app_dir = os.path.join(current_directory, 'dml_app')
                if os.path.exists(source_dml_app_dir):
                    shutil.copytree(source_dml_app_dir, target_dml_app_dir, dirs_exist_ok=True)
                return
            if 'file' not in request.files:
                    return 'No file part', 400
                
            file = request.files['file']
                
            # 如果用户没有选择文件，浏览器可能会发送一个没有文件名的空文件
            if file.filename == '':
                return 'No selected file', 400
                
            if file:
                taskId = self.__next_task_id()
                filename = f"{taskId}_taskFile.zip"
                save_path = os.path.join(dirName,'task_file', filename)
                self.taskFileList[taskId] = {'links_file': filename}
                print(save_path)
                file.save(save_path)
                analyse_file(taskId)

                # 返回成功响应
                return 'File successfully uploaded.', 200
            
            return
        
        @self.testbed.flask.route('/startTask', methods=['GET'])
        def route_start_task():
            """
            用户信息已发送完毕，开始执行用户的任务，主要步骤有：
            1、处理用户文件
            2、分析任务请求
            3、将请求交给Scheduler处理并接收结果
            4、启动Edgetb（包括保存节点信息切割数据集生成对应的structure等步骤）
            5、返回实验结果
            """
            taskId = request.args.get('taskId')
            allocation = task_schedule(taskId)
            edgeTB_start(allocation)

            def task_schedule(taskId: int):
                """
                丢给Scheduler处理，得到gl_run.py里的配置
                """
                allocation = self.testbed.scheduler.resource_schedule(taskId)
                for node, node_info in allocation: 
                    print(f"node name: {node}, node object: {node_info}")
                return allocation
            
            def edgeTB_start(allocation: Dict):
                """
                让edgetb启动相应的容器
                """
                nfsApp = self.testbed.nfs['dml_app']
                nfsDataset = self.testbed.nfs['dataset']
                for node, node_info in allocation:
                    emu = self.testbed.emulator[node_info['emulator']]
                    en = self.testbed.add_emulated_node (node, '/home/qianguo/worker/dml_app/'+str(taskId),
                        ['python3', 'gl_peer.py'], 'dml:v1.0', cpu=16, ram=2, unit='G', emulator=emu)
                    en.mount_local_path ('./dml_file', '/home/qianguo/worker/dml_file')
                    en.mount_nfs (nfsApp, '/home/qianguo/worker/dml_app')
                    en.mount_nfs (nfsDataset, '/home/qianguo/worker/dataset')
            
            def task_finish():
                """
                最终收尾工作
                """
            return
        
   
    
    def analyse_request():
        """
        分析任务请求，发送给Scheduler，接收结果
        """
        return
    
    def send_request_edgetb():
        """
        将请求发送给edgetb   
        """
        return
    
    def __next_task_id(self):
        """
        获取下一个任务id
        """
        self.currtkId += 1
        return self.currtkId