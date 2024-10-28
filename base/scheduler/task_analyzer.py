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
            4、启动Edgetb
            5、返回实验结果
            """
            task_id = request.args.get('taskId')
            def analyse_file():
                """
                处理用户的文件，将压缩包解压并放到不同的文件夹中
                dml_app和dml_file需要和worker挂载，要不直接挂载一个大的文件夹，然后往里面更新算了（
                那挂载可以不动了，直接在下面为task创建对应的文件夹，文件路径改改
                worker_utils可以获得taskId，喜
                """
                zip_filename = f"{task_id}_taskFile.zip"
                zip_path = os.path.join(dirName, "task_file", zip_filename)
                
                current_directory = os.path.join(dirName, "task_file", task_id)
                os.makedirs(current_directory, exist_ok=True)
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(current_directory)
                            
                # os.remove(zip_path)

                # 移动 links.json到task_links 并改名为 {taskId}_links.json
                links_json_path = os.path.join(current_directory, 'links.json')
                target_links_json_path = os.path.join(dirName, "task_links", task_id, "links.json")
                if os.path.exists(links_json_path):
                    shutil.move(links_json_path, target_links_json_path)
                
                # 创建 dml_file 子目录
                target_dml_file_dir = os.path.join(dirName, "dml_file", task_id)
                os.makedirs(target_dml_file_dir, exist_ok=True)
                
                # 移动 dml_file 文件夹到指定目录
                source_dml_file_dir = os.path.join(current_directory, 'dml_file')
                if os.path.exists(source_dml_file_dir):
                    shutil.move(source_dml_file_dir, target_dml_file_dir)

                # 创建 dml_app 子目录
                target_dml_app_dir = os.path.join(dirName, "dml_app", task_id)
                os.makedirs(target_dml_app_dir, exist_ok=True)
                
                # 移动 dml_app 文件夹到指定目录
                source_dml_app_dir = os.path.join(current_directory, 'dml_app')
                if os.path.exists(source_dml_app_dir):
                    shutil.move(source_dml_app_dir, target_dml_app_dir)
                
                return
            
            def analyse_task():
                """
                分析任务需求
                """

            def task_schedule():
                """
                丢给Scheduler处理
                """
            
            def edgeTB_start():
                """
                让edgetb启动相应的容器
                """
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