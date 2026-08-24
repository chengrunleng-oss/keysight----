# Keysight N5171B Python 控制器

这是一个纯 Python、无第三方依赖的 N5171B 控制器。计算机通过网线连接信号源，使用 TCP 5025 端口发送 SCPI 指令。

## 模块结构

```text
n5171b/
|-- connection.py   # TCP 连接、SCPI 指令发送和查询
|-- output.py       # 固定频率、功率和 RF 开关
|-- list_sweep.py   # dwell 检查、线性列表扫描和单次 TTL 触发
|-- controller.py   # 组合以上独立模块
|-- configs.py      # 默认仪器 IP
`-- __init__.py     # 对外 Python 接口
```

旧的步进扫描接口已经删除。列表扫描统一通过 `source.list_sweep` 使用。

## 连接仪器

在 N5171B 前面板依次进入：

```text
Utility > I/O Config > LAN Setup > Manual Config Settings > IP Address
```

如果 `Config Type` 是 `Auto (DHCP/Auto-IP)`，连接网线并等待仪器获得地址后再查看。没有 DHCP 时，可以给仪器和电脑设置同一网段但不重复的静态地址，例如仪器使用 `192.168.10.2`、电脑使用 `192.168.10.1`，子网掩码均为 `255.255.255.0`。

确认仪器的 Socket SCPI 服务已启用：

```text
Utility > I/O Config > LAN Services Setup > SCPI Services
> Sockets SCPI: On > Proceed With Reconfiguration
```

然后连接仪器：

```python
from n5171b import N5171B

source = N5171B("192.168.1.100")
print(source.connect())
source.close()
```

## 设置并检查 dwell

单位统一为秒。下面的代码要求仪器设置 1 ms 列表 dwell，然后读取仪器实际接受的值：

```python
from n5171b import N5171B

with N5171B("192.168.1.100") as source:
    dwell = source.list_sweep.set_and_check_dwell(0.001)
    print("请求值:", dwell.requested_s)
    print("量化值:", dwell.programmed_s)
    print("仪器回读值:", dwell.actual_s)
    print("仪器最小值:", dwell.minimum_s)
    print("是否完全相同:", dwell.exact)
```

程序会先向仪器写入手册给出的最小值 `100 us`，再读回确认。因此，这里的最小值是当前仪器实际接受并返回的值。列表 dwell 按手册给出的 `1 us` 分辨率向上量化，最大值为 `100 s`；如果仪器报错或回读值与写入值不一致，程序会抛出异常。

也可以只读取仪器对手册最小值的实际响应：

```python
minimum_dwell_s = source.list_sweep.measure_minimum_dwell()
```

## 立即完成一次线性列表扫描

```python
from n5171b import N5171B

with N5171B("192.168.1.100") as source:
    result = source.list_sweep.run_linear_sweep(
        start_mhz=100,
        stop_mhz=1000,
        points=101,
        sweep_time_s=1.01,
        direction="forward",
    )

    print("列表排列:", result.start_mhz, "->", result.stop_mhz)
    print("实际扫描:", result.output_start_mhz, "->", result.output_stop_mhz)
    print("每点实际 dwell:", result.actual_dwell_s)
    print("实际写入的 dwell 总时间:", result.programmed_dwell_time_s)
```

`start_mhz` 和 `stop_mhz` 始终决定频率表从点 1 到点 N 的排列，两者谁大谁小都可以。`direction` 决定仪器沿点号正着还是反着扫描：

频率参数的单位是 MHz，N5171B 的最低频率是 `0.009 MHz`（9 kHz）。例如 `1e-6 MHz` 等于 1 Hz，并不是 1 MHz；程序会在通讯前拒绝低于 9 kHz 的起止频率，避免仪器静默钳位后再产生列表错误。

| 参数 | SCPI 方向 | 实际扫描 | 扫描后点号 |
|---|---|---|---:|
| `direction="forward"` | `LIST:DIR UP` | `start_mhz -> stop_mhz` | N |
| `direction="reverse"` | `LIST:DIR DOWN` | `stop_mhz -> start_mhz` | 1 |

程序固定使用 `LIST:MODE AUTO` 和 `LIST:RETR OFF`，不使用 MAN 模式重置点号。正向扫描结束后保持在点 N，下一次反向扫描直接从点 N 开始；反向扫描结束后保持在点 1，下一次正向扫描直接从点 1 开始。

每次更换扫描表时，程序按以下三个阶段执行：

```text
1. ABOR，读取旧列表当前点的频率，写入 FREQ:CW，然后执行 FREQ:MODE CW
2. 保持 CW 模式，逐条写入 LIST 的类型、模式、回扫、频率、dwell 和方向
3. 列表回读校验通过后，单独执行 FREQ:MODE LIST，再执行 INIT
```

`FREQ:MODE CW` 只关闭频率扫描，不会关闭 RF 输出。切换到 CW 前写入的频率来自旧列表当前点，因此编辑新列表时应继续保持原来的端点频率。切回 LIST 后，输出才转到新列表所选方向的起始点。

本分支针对网线通讯次数进行了优化：同一阶段内的写命令及其状态回读组成一条复合 SCPI 消息。首次实机回读最小 dwell 后，控制器会缓存该值，后续扫描不再重复测量。切入 CW、写入列表、切回 LIST 仍是三个独立阶段，写表失败时不会启用不完整的列表。首次立即扫描通常需要 7 次网络交换，后续扫描通常需要 6 次；具体耗时仍取决于仪器处理列表和扫频所需的时间。

### 连续交替不同扫描范围

下面三次调用的实际输出依次为 `5 -> 10 MHz`、`15 -> 20 MHz`、`25 -> 30 MHz`：

```python
common = {"points": 101, "sweep_time_s": 1.01}

source.list_sweep.run_linear_sweep(
    start_mhz=5, stop_mhz=10, direction="forward", **common
)
source.list_sweep.run_linear_sweep(
    start_mhz=20, stop_mhz=15, direction="reverse", **common
)
source.list_sweep.run_linear_sweep(
    start_mhz=25, stop_mhz=30, direction="forward", **common
)
```

第二张表必须排列为 `20 -> 15 MHz`，因为反向扫描会从它的最后一点 `15 MHz` 扫到第一点 `20 MHz`。

每次写表前，程序都会在 AUTO 模式下检查 `LIST:CPO?`，并从旧的 `LIST:FREQ?` 中读取该点的保持频率：

- 正向扫描要求当前点为 `1`。
- 反向扫描要求当前点等于本次传入的 `points`。
- 状态不符合时，函数在写入新频率表之前抛出异常。

因此，正向扫描后接反向扫描时，点数不能随意增加。例如上一次正向扫描使用 101 点并停在点 101，下一次反向扫描也必须从点 101 开始。反向扫描后停在点 1，接下来的正向扫描可以使用不同点数。

第一次接管仪器、扫描被中止、TTL 尚未到来、连接重建或前面板状态被修改时，当前点不一定在所需端点。程序不会猜测或自动归位，而是报告当前点和所需点。不要在正常的正反交替扫描之间调用 `abort()`。

这个函数会生成线性表、检查并量化 dwell、回读列表长度和 dwell，并在立即扫描完成后确认 AUTO 点号确实停在预期终点。默认 `rf_on=True` 不关闭 RF；显式传入 `rf_on=False` 才会关闭 RF。频率切换仍有仪器自身的瞬态，软件不能保证相位连续。

`sweep_time_s` 表示所有扫描点的 dwell 之和，不是从第一点到最后一点的精确墙钟时间：

```text
实际扫描时间 = 所有点 dwell 之和 + 各点处理、切换和稳定时间
```

因此，`sweep_time_s` 必须至少为 `points * minimum_dwell_s`。函数不修改当前固定输出功率，默认打开 RF 输出；传入 `rf_on=False` 可以让扫描过程保持 RF 关闭。

## 等待一个外部 TTL 触发

```python
from n5171b import N5171B

with N5171B("192.168.1.100") as source:
    result = source.list_sweep.arm_linear_sweep_for_trigger(
        start_mhz=100,
        stop_mhz=1000,
        points=101,
        sweep_time_s=1.01,
        direction="forward",
        trigger_input="TRIG1",
        edge="POS",
    )

    # 此时仪器已经进入等待状态。
    # TRIG1 上的一个 TTL 上升沿会启动整张频率表。
```

这里使用两个不同层级的触发设置：

- 整次扫描触发源为外部 `TRIG1`。
- 列表内部的点触发源为 `IMM`，收到一个外部沿后，仪器按 dwell 自动扫完整张表。
- `INIT:CONT OFF` 保证只执行一次。扫完后保持最后一个频点，不会自动重新等待第二次触发。
- 进入等待状态前，程序确认 AUTO 当前点就是所选方向的起始端点。等待期间 RF 保持在该频率。

要再次等待一个触发，需要在上一段扫描完成后，以相反的 `direction` 再次调用 `arm_linear_sweep_for_trigger()`。`edge="NEG"` 可改为下降沿，`trigger_input` 还可以选择 `TRIG2` 或 `PULSE`。

中止正在运行或等待触发的扫描：

```python
source.list_sweep.abort()
```

## 固定频率和功率

```python
from n5171b import N5171B

with N5171B("192.168.1.100") as source:
    source.output.set_single_point(
        frequency_mhz=1000,
        power_dbm=-10,
        rf_on=True,
    )

    print(source.output.get_state())
```

## 参考资料

项目使用的 Keysight 官方手册保存在 `references/keysight/`。资料用途、重点页码、官方来源和校验值见 [references/README.md](references/README.md)。列表 dwell、回扫和触发命令主要参考 SCPI Command Reference 第 84-90 页及第 242-245 页。
