# AX9000(NSS) 多拨整形:初步探索

范围:只读源码/文档调研,未接触任何路由器。"推断"= 无直接出处的判断。
上游源码镜像:CodeLinaro-mirror/qsdk_oss_lklm_{nss-clients,nss-drv,qca-nss-ecm,nss-dp}(GitHub raw);包定义 qosmio/nss-packages。

## 1. f9bf966 结论是否正确:基本正确

**(a) NSS qdisc 拒绝 NSS 固件不认识的 netdev,macvlan 拨号口属于此类。**
- nss-clients 51be82d `nss_qdisc/nss_qdisc.c`:非 root 路径 2221-2227、bridge root 路径 2324-2330、接口 root 路径 2391-2397。三处都调用 `nss_cmn_get_interface_number()`,返回 <0 就报 "unknown to nss driver" 并置 `INIT_FAILED`。注释写明 "We do NOT support non-NSS known interfaces"。
- `nss_cmn_get_interface_number()` 只在 `subsys_dp_register[i].ndev` 里查找已注册的 netdev(nss-drv `nss_cmn.c:72-96`)。
- nss-clients 目录里没有 macvlan 管理器,只有 vlan、bridge、pppoe、lag 等(树列表 `.../git/trees/51be82d?recursive=1`)。nss-drv 里也没有 macvlan 文件。所以 macvlan 不会被注册,这一步是推断。

**(b) nssmirred 对未知 netdev 返回 `-ENODEV`。**
- `nss_qdisc/igs/nss_mirred.c:138-141`:`nss_cmn_get_interface_number_by_dev(from_dev) < 0` 时返回 -ENODEV。
- 本仓库的 ingress 以 dialN 作为 from_dev 做 mirred 到 ifb4dialN,所以会失败。
- 因此 8f7087a 的 per-dial nsstbl/nssfq_codel 确实装不上。

**(c) ECM 只加速 private 模式的 macvlan。**
- qca-nss-ecm `ecm_interface.c`(30fbfa4):`ecm_interface_macvlan_mode_is_valid()` 在 2993-3007,只放行 `MACVLAN_MODE_PRIVATE`。
- 建立接口层级时,3156-3170 对非 private 的 macvlan `return NULL`;5267-5288 则 `goto done`。
- VIKINGYFY 固定的 ECM 8c7355b 里同样的判断在 `ecm_interface.c:3410`。
- 该代码受 `ECM_INTERFACE_MACVLAN_ENABLE` 控制,仅当 kmod-macvlan 被选中时才开启(`qca-nss-ecm/Makefile:114-115`、`240-242`)。
- 补充(推断):vepa 流量不是被"忽略",而是 ECM 建不起接口层级、加速失败后留在内核。每条新流会多付一次失败的建连开销。f9bf966 的 live 脚本用 `ecm_v4_exception_stats` 计数,正是在量这个开销。

**(d) 新发现的风险(推断)。**
- nss-dp 的 netdev ops 里没有 `ndo_set_rx_mode`、`ndo_bpf`,也没有 `IFF_UNICAST_FLT`(`nss_dp_main.c:409-426`)。
- 内核会把物理 wan 切到混杂模式来收各拨号 MAC。
- 交换机/GMAC 层是否仍会按目的 MAC 丢弃,必须实机验证。

## 2. 全 CPU 路径的吞吐预算

- **没有找到发布的 IPQ807x 上 CAKE 干净基准。** 只有间接数据:
  - DL-WRX36(IPQ8072A,非 NSS 主线构建)800 Mbit CAKE MQ 的 issue。报告者凭记忆说 >700 Mbps,另一次实测下载 647 Mbps(openwrt/openwrt#22344 的评论)。该 issue 本身是 PR22303 之后的速度回退,数据质量低。
  - 论坛泛称无 NSS 时纯路由约 500-600 Mbps(forum.openwrt.org/t/…/81000 第 6 楼只说 "300-500 Mbit/s 取决于 SoC",500-600 是搜索摘要转述,未核实)。
- **CAKE 是单核串行。** cake-autorate README:"A CAKE qdisc is run on a specific CPU core",CPU 饱和时带宽会被压低(`README.md` "CPU usage monitoring")。
- 推断:5-8 个独立 CAKE(egress + ifb)可以分摊到 4 个核,所以总量不受单核限制。
  - 单向 5×100=500 Mbit,CAKE+ifb 双向约 1 Gbit 的 CAKE 处理量。大概率可行,但没有数据支撑。
  - 8×100 达到 800 Mbit 往返,临界,必须实测。
  - 校园出口总带宽若不足 800 Mbit,瓶颈会先出现在别处。
- **RPS/IRQ。** ECM 的 init 会主动关闭 OpenWrt packet steering(`qca-nss-ecm.init:71-76,183,189`)。
  - 推断:CPU 路径的 softirq 可能集中在少数核,需手动配置 `rps_cpus`/IRQ affinity。
  - ifb 的 mirred 在 rx softirq 内执行,egress CAKE 在发送方所在核运行。
- **"只对拨号流关闭 ECM、其他流保持卸载"不需要额外旋钮。** vepa 本身就让拨号流不被加速(见 1c)。
  - 其余可用的旋钮粒度都更粗:denied ports 是全局按端口(patch 0021/0024),`ecm_classifier_mark/nl/pcc` 是需要外部内核模块或 netlink 的逐流分类器。
  - 没有找到"按接口"的旋钮。

## 3. 备选设计

| 方案 | 可行性 | 评价 |
|---|---|---|
| (a) 每拨号 CPU CAKE + cake-autorate(现状) | 可行 | 保留逐拨号上限保护,避免超过校园每 MAC 限速(约 100M)。代价是 CPU 和 N 个 autorate 实例 |
| (b) 物理 WAN 上一个 NSS 聚合整形 + autorate 改 mwan3 权重 | 部分 | 无法对单拨号设上限,权重只调流量分配,不能防单拨号超限(推断)。ingress 要 IGS/nssmirred,且 wan 在 NSS 里是已知口,安装可行。mwan3 权重改动要 reload,响应慢(推断) |
| (c) cake-autorate 控制物理口 nsstbl 速率 | 技术可行,价值低 | 需改 autorate 的 `tc change` 调用:`cake-autorate.sh:772-801` 写死 `tc qdisc change … cake bandwidth`。聚合整形同样不能代表逐拨号容量 |
| (d) 用 NSS 可识别的设备取代 macvlan | 基本不可行 | NSS 认识 VLAN(`nss-clients/vlan/nss_vlan_mgr.c`),但 VLAN 子接口共享父口 MAC,而门户按 MAC 登录;还要求校园网承载 VLAN(推断)。PPPoE 不适用 |
| (e) eBPF/XDP | 无收益 | nss-dp 无 `ndo_bpf`,原生 XDP 不可用。tc-bpf/EDT 挂在 dialN 上仍是 CPU 路径,和 CAKE 同类成本,是否更省 CPU 属推断 |

另一种思路(推断):把拨号设为 private 并让 ECM 加速,但这样 CAKE 被绕过,且 NSS 仍不能整形 macvlan,没有净收益。

## 4. cake-autorate 的成本

- 每个实例有 5 个进程:`main`、`intercept_stderr`、`maintain_log_file`、`fping_pinger`、`monitor_achieved_rates`(README 的 `proc_pids` 示例)。
- CPU 与 ping 响应频率成正比。默认 20 Hz(6 个 pinger、间隔 0.3s);减到 3 个 pinger 约省一半 CPU(README)。
- 论坛数据(搜索摘要转述,未核实):RT3200 上活跃约 9-12%,睡眠约 2-3%。
- 推断:按线性外推,8 个实例在 A53 上约占 1 核的 0.7 倍(活跃,默认参数)。降低 pinger 数量、关闭 `output_summary_stats`/`output_processing_stats`/`output_load_stats` 可明显下降。但本仓库 `sqm.py:226` 为了 LuCI 统计打开了 SUMMARY,这部分要权衡。
- 本仓库已有 `enable_sleep_function` 与 420s 空闲阈值(`docs/cake-autorate-idle-recovery.md`),闲置拨号只占睡眠成本。
- 更轻的实现(均为社区项目,未实测、未核实质量):
  - `Lochnair/sqm-autorate-rust`(现名 unRAVEL):只改已有 root CAKE 的带宽。
  - `woffko/cake-autorate-rs-owrt`:Rust,含 Multi-WAN。
  - `kamikaonashi/openwrt-package-darkmoon`:C 重写,自称 bringup。
  - `ooonea/cakewalk`:专注 ingress,无稳定版。
  - CAKE 内核自带 `autorate-ingress`:官方 wiki 标注 "very experimental",论坛反馈会把带宽估得过高。
- 推断:没有现成的"多拨号共享一个测量进程"的方案。反射器共享需要自己写,每拨号仍必须经自己的接口(`-I dialN`)探测,因为各拨号路径独立。

## 5. 推荐方向与待测项

**推荐:保持 (a),并把整形的 CPU 成本当作要测的变量,不要预设 NSS 能接管。**
- 理由:NSS 无法整形 macvlan(第 1 节),备选方案要么丢失逐拨号上限,要么没有净收益。
- 以 f9bf966 的 vepa + CPU CAKE 为基线,先量出余量,再决定是否需要降 pinger、改 RPS,或换更轻的 autorate。

**必须在真机 AX9000 上测量:**
1. wan 上收各拨号 MAC 的收包是否正常(混杂模式 / MAC 过滤 / 第 1d 节风险),单拨号和多拨号各一次。
2. 全 vepa 下 ECM 异常计数(`ecm_v4_exception_stats`)的增长速度和新建连的 CPU 开销;可直接用 `tests/live/ax9000_nss_coexist_check.sh`。
3. N=1/3/5/8 拨号,每拨号 50/100 Mbit,iperf3 上下行同时跑。记录每核 `/proc/stat` softirq、`mpstat -P ALL`、`/proc/interrupts`、`/proc/softirqs`。判断 CAKE 是否饱和:`tc -s qdisc` 的 drops/backlog,以及吞吐是否被压低(README 所说的饱和现象)。
4. 单个 CAKE 实例(egress 与 ifb 各自)的最大速率。用 `tc qdisc` 无限速跑纯 CAKE,确定单核上限。
5. RPS/IRQ 对比:ECM 默认关闭 packet steering,对比手动设置 `rps_cpus`、IRQ affinity 前后的上述指标。
6. cake-autorate 实例数 5/8 时的 CPU,默认 vs 3 个 pinger vs 关闭 SUMMARY,用 `output_cpu_stats`。
7. 延迟:负载下 per-dial 的 ping 抖动和 cake-autorate 稳定性,对比 CPU 饱和与不饱和两种情况。
8. NSS 与 CPU 路径共存:LAN↔LAN 与 LAN↔非拨号流的 ECM 加速不受影响;wan 上无 NSS qdisc(`offload_warnings` 的检查)。

**无需硬件即可做的:**
- 在 x86 或任意 OpenWrt 虚机上建 macvlan(vepa)+ CAKE + ifb,压 N 个实例,测软件层面的 cake-autorate CPU 与 CAKE 并行扩展性,仅作相对参考,不能代表 A53。
- 复核 sqm.py 的 `offload_warnings` 解析逻辑(已有单元测试)。
- 在虚机上评估轻量 autorate(Rust/C 版本)与 cake-autorate 的 CPU 对比。
- 代码层面:设计 autorate 后端抽象(`tc change` 调用点)以便将来替换。
