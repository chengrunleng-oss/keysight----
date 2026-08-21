# N5171B 参考资料

本目录保存本项目使用的公开资料。PDF 均从 Keysight 官方网站下载，下载日期为 2026-08-21。

## 官方文档

| 本地文件 | 页数 | 用途 |
| --- | ---: | --- |
| `keysight/N5171B_X-Series_Programming_Guide_9018-03987.pdf` | 398 | 远程控制、LAN 配置、SCPI 编程流程和示例 |
| `keysight/N5171B_X-Series_SCPI_Command_Reference_9018-03690.pdf` | 581 | N5171B 支持的 SCPI 命令、参数及适用型号 |
| `keysight/N5171B_X-Series_Users_Guide_9018-03689.pdf` | 479 | 后面板接口、步进扫频、触发及前面板设置 |
| `keysight/N5171B_N5172B_Data_Sheet_5991-0039.pdf` | 39 | N5171B 频率范围、接口和硬件规格 |
| `keysight/USB_LAN_GPIB_Connectivity_Guide_E2094-90009.pdf` | 234 | 仪器与计算机之间的 LAN、USB、GPIB 通用连接方法 |

## 与当前代码最相关的位置

- SCPI Command Reference：List/Sweep 子系统约在 PDF 第 80-95 页，其中包含步进扫频、驻留时间、点触发源及触发沿命令。
- SCPI Command Reference：Trigger 子系统约在 PDF 第 241-245 页，其中包含整次扫频触发源、TRIG 1/TRIG 2 和触发沿命令。
- User's Guide：后面板接口约在 PDF 第 29-41 页；步进与列表扫频约在 PDF 第 66-73 页。
- Programming Guide：远程操作及 LAN 接口位于前半部分，可用于确认网线控制能力和连接配置。
- Connectivity Guide 是较早的通用连接文档，只用于网络连接背景；具体命令以 N5171B SCPI Command Reference 为准。

## 官方来源

- Programming Guide: <https://www.keysight.com/us/en/assets/9018-03987/programming-guides/9018-03987.pdf>
- SCPI Command Reference: <https://www.keysight.com/us/en/assets/9018-03690/programming-guides/9018-03690.pdf>
- User's Guide: <https://www.keysight.com/us/en/assets/9018-03689/user-manuals/9018-03689.pdf>
- Data Sheet: <https://www.keysight.com/zz/en/assets/7018-03381/data-sheets/5991-0039.pdf>
- Connectivity Guide: <https://www.keysight.com/kr/ko/assets/9018-03650/user-manuals/9018-03650.pdf>
- N5171B 官方支持页: <https://www.keysight.com/us/en/support/N5171B/exg-x-series-rf-analog-signal-generator-9-khz-6-ghz.html>

## 公开 Python 参考项目

以下项目可作为通用 Python/SCPI 设计参考，但不是 N5171B 命令的权威来源，本项目未复制其代码：

- pyscpi: <https://github.com/eelab-dev/pyscpi>，MIT 许可，演示了通过 TCP 5025 端口连接 SCPI 仪器。
- PyArbTools: <https://pyarbtools.readthedocs.io/en/latest/readme.html>，面向 Keysight 信号源的 Python 控制和波形工具。

第三方代码示例只能用于理解连接方式。N5171B 的命令名称、参数范围和触发行为应以本目录中的官方 SCPI Command Reference 为准。
