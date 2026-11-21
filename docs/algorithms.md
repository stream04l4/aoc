# AOC V2.0 — 核心驱动器、检索、自模型与路由算法

所有权重、阈值与衰减常数均外置在 `config/drives.yaml`、`config/memory.yaml`、`config/self_model.yaml` 与 `config/affect.yaml` 中，禁止硬编码。

---

## 1. 统一 `DriveState` 模型与三大独立驱动器

每个驱动器独立输出归一化至 `[0, 1]` 的 `DriveState`（`trend ∈ [-1, 1]` 除外），且严格满足派生不变量 `deficit = 1.0 - satisfaction`：

1. **Survival（`SurvivalDriveEvaluator`，P1-1 解耦重构）**：
   - 拆分为正交维度：`resources`（磁盘、内存非线性评分与持续每核负载 `load_5m / cores` 的瓶颈加权 `0.4*mean + 0.6*min`）、`llm_health`（共享 `LLMGateway.health_check`）、`agent_availability`、`reliability`。
   - **经济配额解耦**：每日任务/Token 预算作为独立 `QuotaStatus`（`QUOTA_OK` / `QUOTA_EMERGENCY_ONLY` / `QUOTA_EXHAUSTED`）管理，耗尽时进入 `THROTTLED` 休眠至 `quota_reset_at`，绝不触发虚假的 `Survival CRITICAL`。
2. **Reproduction（`ReproductionDriveEvaluator`，P1-7 实证重构）**：
   - **彻底根除静态能力泄漏（T10）**：外部 Agent 声明的静态能力对 `capability_coverage` 的贡献恒为 **`0`**；
   - 仅统计 `capability_profiles(maturity_status IN ('ACQUIRED', 'VALIDATED'))`、SHA-256 校验通过的 `skill_registry(status='VALIDATED')`、近期完成的能力建设目标、资源储备与知识可迁移就绪度。
3. **Curiosity（`CuriosityDriveEvaluator` + P1-4 四因子信息增益）**：
   - 综合未解知识空白（`knowledge_gaps`）、外部感知新话题（`unknown_information`）、探索完成率、近期新语义记忆、时间衰减的实测信息增益（`information_gain`）与学习进度。

---

## 2. 混合驱动仲裁与可选有界 Affect 调制（P1-9）

对每个驱动器 $d$：

$$\text{base}_d = \text{deficit}_d \cdot \text{urgency}_d \cdot \text{confidence}_d \cdot w_d$$

$$\text{inhibition}_d(t) = 1 - s_{\text{inhib}} \cdot \exp\left(-\frac{t - t_{\text{sat},d}}{\tau_{\text{inhib}}}\right)$$

$$\text{priority}_d = \max\Big(0,\; \text{base}_d \cdot (1 + \alpha \cdot \text{persistence}_d) \cdot \text{inhibition}_d(t) - \beta \cdot \text{cost}_d - \gamma \cdot \text{risk}_d + \Delta_{\text{affect},d}\Big)$$

- 当 `config/affect.yaml` 默认 `enabled: false` 时，$\Delta_{\text{affect},d} \equiv 0$。
- 当开启 Affect 时，$|\Delta_{\text{affect},d}| \le \text{maximum\_influence} \le 0.15$，且对 `survival` 驱动器恒有 $\Delta_{\text{affect,survival}} \equiv 0$，绝不干预 `Survival CRITICAL` 紧急接管、L0–L3 治理、预算与审批。

---

## 3. 五维混合语义检索算法（P1-4 `HybridRetrievalEngine`）

给定查询 $q$ 与候选记忆 $m$，综合得分由五项归一化分量的加权和给出（默认权重配置于 `config/memory.yaml`）：

$$\text{Score}(q, m) = 0.45 \cdot S_{\text{vec}}(q, m) + 0.20 \cdot S_{\text{trgm}}(q, m) + 0.15 \cdot S_{\text{rec}}(m) + 0.10 \cdot S_{\text{rel}}(m) + 0.10 \cdot S_{\text{imp}}(m)$$

- $S_{\text{vec}}$：同一 `model_id` 下的余弦相似度（PostgreSQL `pgvector` `<=>` 算子）；
- $S_{\text{trgm}}$：中英文双语字符/词项 Trigram 相似度（PostgreSQL `pg_trgm`）；
- $S_{\text{rec}} = 2^{-\Delta t / T_{1/2}}$：基于半衰期（默认 168 小时）的新鲜度衰减；
- $S_{\text{rel}}$ 与 $S_{\text{imp}}$：来源可靠度与归一化重要度。

---

## 4. 四因子确定性信息增益（P1-4 `InformationGainEvaluator`）

$$\text{InfoGain} = \text{Novelty} \times \text{SourceReliability} \times \text{VerificationConfidence} \times \text{GoalRelevance}$$

- 若新内容与已有记忆余弦相似度 $\ge 0.92$（高度重复）：$\text{Novelty} = 0$（若来自独立高可靠源则仅赋予最高 $0.35$ 的交叉印证微幅增益）；
- 若确定性验证未通过（`verification_passed = False`）：$\text{VerificationConfidence} = 0 \implies \text{InfoGain} = 0$。

---

## 5. 动态 Self Model Beta-Binomial 能力后验（P1-7）

对每项能力 $c$，以保守均匀分布 $\text{Beta}(\alpha_0=1.0, \beta_0=1.0)$ 为先验，根据不可变账本 `capability_evidence` 中的实证成功权重 $W_{\text{succ}}$ 与失败权重 $W_{\text{fail}}$ 更新后验：

$$\alpha = \alpha_0 + W_{\text{succ}}, \quad \beta = \beta_0 + W_{\text{fail}}, \quad \hat{p} = \frac{\alpha}{\alpha + \beta}$$

通过正则化不完全 Beta 分布反函数 $I_x^{-1}(\alpha, \beta)$ 计算 90% 单侧置信下界 $p_{\text{low}} = Q_{\text{Beta}}(0.10; \alpha, \beta)$：
- 仅当样本量 $N \ge \text{minimum\_samples} (3)$ 且 $p_{\text{low}} \ge 0.55$（或关联至少 1 个校验和完好的 `VALIDATED` Skill）时，能力方可晋升为 `VALIDATED`。

---

## 6. 五因子可解释执行路由（P1-6 `AgentRouter`）

$$\text{RouteScore}(a) = \text{CapabilityMatch}(a) \times \text{EmpiricalReliability}(a) \times \text{Availability}(a) \times \text{CostEfficiency}(a) \times \text{RiskCompatibility}(a)$$

每次路由均将所有候选 `AgentProvider` 的五项分值与最终选择理由持久化至 `decision_logs(decision_type='execution_routing')`。
