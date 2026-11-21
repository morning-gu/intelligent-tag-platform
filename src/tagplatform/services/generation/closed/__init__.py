"""封闭式生成（模式 A）子包：producer / recall / parse。

三个模块互为协议相邻件：recall 的候选行供 producer 按目录分组注入
prompt；parse 的输出契约（嵌套三模态 evidence）与 producer 的打分消费
咬合，文案与解析互为协议。导入 `producer` 即注册 closed 生成器。
"""
