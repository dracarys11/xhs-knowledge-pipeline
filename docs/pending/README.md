# Pending Problems Archive

本目录是 P1 阶段冻结后的已知限制与边界案例档案。

**定位与原则**：
- **不是 Issue Tracker**：不用于日常缺陷跟踪。
- **不是 TODO List**：不作为下一阶段默认开发清单。
- **定位**：冻结后的已知限制、未证实边界与真实网络特征的轻量记录。
- **重开门槛**：仅在出现数据损坏（Data Corruption）、状态机回归（Resume Regression）或数据库覆盖受损时才可重新打开。

## 档案清单

- [P1_REMOTE_COVERAGE_PROOF.md](P1_REMOTE_COVERAGE_PROOF.md): 收藏列表远端枚举触底信号未证实 (Exit Code 3)
- [P1_FINAL_FAILED_TIMEOUT.md](P1_FINAL_FAILED_TIMEOUT.md): 偶发网络导航超时导致尝试次数耗尽 (FINAL_FAILED)
- [P1_LARGE_MEDIA_RECOVERY.md](P1_LARGE_MEDIA_RECOVERY.md): 极端大媒体文件与弱网断点未触发测试
