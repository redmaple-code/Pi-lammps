# 计算软件开源与部署核对

核对日期为2026-11-17，部署范围为小分子对接。当前选择 Windows 原生 Vina 和独立 Python 虚拟环境。

## 实际部署链

| 软件 | 安装版本 | 许可证 | 用途与依据 |
|---|---|---|---|
| AutoDock Vina | 1.2.7 | Apache-2.0 | 官方发布版执行真实搜索，版本输出和官方LICENSE已保存 |
| RDKit | 2026.09.2 | BSD-3-Clause | 分子身份、初态构象、参考RMSD和结构处理，安装包许可证已保存 |
| Meeko | 0.8.0 | LGPL-2.1 | 受体/配体PDBQT准备和结果分子恢复，官方及安装包LICENSE已保存 |
| Gemmi | 0.7.5 | MPL-2.0 | 结构处理依赖，安装包LICENSE已保存 |
| NumPy | 2.4.6 | BSD系及安装包列出的第三方许可证 | 数值数组与坐标分析，具体组成见安装元数据 |
| SciPy | 1.17.1 | BSD-3-Clause及随附第三方条款 | Meeko/科学计算依赖 |
| Matplotlib | 3.11.2 | Matplotlib许可证 | 分析图和实际坐标叠加图 |

这些工具提供开源许可。许可证文件、版权通知和第三方组成随安装记录保存，后续分发软件时按对应版本条款处理。

Vina取自官方v1.2.7 Windows发布附件。Python包通过清华PyPI镜像安装，21个wheel的SHA-256均逐项与PyPI官方对应版本元数据匹配。[Vina官方许可](https://github.com/forlilab/AutoDock-Vina/blob/v1.2.7/LICENSE) [Meeko官方许可](https://github.com/forlilab/Meeko/blob/v0.8.0/LICENSE) [RDKit许可](https://github.com/rdkit/rdkit/blob/master/license.txt)

本地证据可以直接查看。[下载记录](evidence/downloads.json) [wheel核对](evidence/wheel-integrity.json) [安装许可证清单](evidence/installed-license-inventory.json) [版本锁](requirements.lock.txt)

## 前一轮有限元候选的核对

| 软件 | 开源情况 | 当前部署范围 |
|---|---|---|
| CalculiX | GPLv2系列，官方Windows源码分发包含GPL许可 | 本次核对许可 |
| Gmsh | GPLv2或更高版本，含外部库链接例外 | 本次核对许可 |
| LS-DYNA | 商业专有软件，需要对应Ansys许可证 | 可作为另行配置的商业求解器 |

有限元包可选择CalculiX/Gmsh开源路线。候选软件的许可证情况和部署能力在工具配置中分别记录。[CalculiX许可](https://github.com/calculix/CalculiX-Windows/blob/master/LICENSE) [Gmsh官方说明](https://gmsh.info/) [LS-DYNA许可要求](https://ansyshelp.ansys.com/public/Views/Secured/corp/v252/en/lsdyna_ug/exd_ag_lsdyna_licensing.html)

## 安装与准备过程中的实际问题

首次pip访问遇到TLS/代理错误，随后官方大文件下载迟缓。记录保留在pip-install.attempt01.log和attempt02.log。成功安装采用当前子进程中的NO_PROXY配置和镜像，官方摘要核对记录了实际来源一致性。

Meeko0.8.0的`-i`入口要求额外ProDy依赖。按本版本帮助改用`--read_pdb`入口。之后遇到教程PDB中SER438原子记录中断，按现有原子记录分组排序，核对原子集合与坐标保持一致后准备成功。相关失败和修复均保留在logs及evidence目录。
