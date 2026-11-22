# AOC V4.0 — 核心驱动器、检索、自模型、路由与 RSI 进化算法

所有驱动器权重、冷却阈值、情绪上限及进化门禁均配置于 `config/*.yaml` 及 `KernelV3Settings`（`app/core/config.py`），禁止在运行期被外接技能或 Playbook 篡改。

---

## 1. 统一 `DriveState` 模型与三大独立驱动器

每个驱动器独立输出归一化至 `[0, 1]` 的 `DriveState`（`trend ∈ [-1, 1]` 除外），且满足 `deficit = 1.0 - satisfaction`：

1. **Survival（`SurvivalDriveEvaluator`）**：
   - 正交拆分运行健康度、物理资源（磁盘/内存非线性评分与每核负载 `load_5m / cores` 瓶颈加权 `0.4*mean + 0.6*min`）、LLM 可用性、Agent 可用性与近期任务可靠度。
   - **经济配额解耦**：每日任务/Token 配额作为独立 `QuotaStatus`（`QUOTA_OK` / `QUOTA_EMERGENCY_ONLY` / `QUOTA_EXHAUSTED`）管理，耗尽时切换至 `THROTTLED` 休眠等待 `quota_reset_at`。
2. **Reproduction（`ReproductionDriveEvaluator`）**：
   - 外部 Agent 静态宣称能力对 `capability_coverage` 贡献恒为 **`0`**。
   - 仅依据 `capability_profiles(maturity_status IN ('ACQUIRED', 'VALIDATED'))`、经 SHA-256 校验通过的 `VALIDATED` Skill 数量、近期完成的能力建设目标、资源冗余与知识迁移就绪度计算。
3. **Curiosity（`CuriosityDriveEvaluator`）**：
   - 综合开放知识空白（`knowledge_gaps`）、外部感知新话题（`unknown_information`）、探索完成率、近期新增语义记忆、时间衰减的实测信息增益（`information_gain`）与学习进展。

---

## 2. 混合驱动仲裁与全闭环有界 Affect 调制（V2.0 + V3.1）

对每个驱动器 $d$：

$$\text{base}_d = \text{deficit}_d \cdot \text{urgency}_d \cdot \text{confidence}_d \cdot w_d$$

$$\text{inhibition}_d(t) = 1 - s_{\text{inhib}} \cdot \exp\left(-\frac{t - t_{\text{sat},d}}{\tau_{\text{inhib}}}\right)$$

$$\text{priority}_d = \max\Big(0,\; \text{base}_d \cdot (1 + \alpha \cdot \text{persistence}_d) \cdot \text{inhibition}_d(t) - \beta \cdot \text{cost}_d - \gamma \cdot \text{risk}_d + \Delta_{\text{affect},d}\Big)$$

- **安全硬约束**：$|\Delta_{\text{affect},d}| \le \text{maximum\_influence} \le 0.15$，对 `survival` 恒有 $\Delta_{\text{affect,survival}} \equiv 0$。
- **V3.1 闭环调制扩展**：
  - `self_model_confidence` 与 `prediction_surprise`（由后文 IG 预测误差驱动）注入情绪更新；
  - `AffectModulation` 在允许边界内输出 `reflection_interval_factor`（微调 `OrganismScheduler` 休眠节奏）与 `retry_backoff_multiplier`（微调 `AgentOrchestrator` 重试退避），异常即降级为 `1.0`；
  - `affect_state` 只读注入 `CognitionBrain`，用于调整探索与谨慎偏好（无权限改动策略）。

---

## 3. 五维混合语义检索算法（`HybridRetrievalEngine`）

给定查询 $q$ 与候选记忆 $m$（默认权重配置于 `config/memory.yaml`）：

$$\text{Score}(q, m) = 0.45 \cdot S_{\text{vec}}(q, m) + 0.20 \cdot S_{\text{trgm}}(q, m) + 0.15 \cdot S_{\text{rec}}(m) + 0.10 \cdot S_{\text{rel}}(m) + 0.10 \cdot S_{\text{imp}}(m)$$

- $S_{\text{vec}}$：同 `model_id` 下余弦相似度（PG `pgvector` `<=>` 算子）；
- $S_{\text{trgm}}$：中英文 Trigram 相似度（PG `pg_trgm`）；
- $S_{\text{rec}} = 2^{-\Delta t / T_{1/2}}$：时间衰减（默认半衰期 $T_{1/2} = 168\text{h}$）；
- $S_{\text{rel}}, S_{\text{imp}}$：来源可靠度与归一化重要度。当 Embedding 或 Letta 离线时自动平滑降级为词汇检索，零数据丢失。

---

## 4. 四因子信息增益与预测误差校准（`InformationGainEvaluator`）

$$\text{InfoGain} = \text{Novelty} \times \text{SourceReliability} \times \text{VerificationConfidence} \times \text{GoalRelevance}$$

- 余弦相似度 $\ge 0.92$（高度重复）：$\text{Novelty} = 0$（独立高可靠跨源最多赋予 `0.35` 交叉印证微幅加权）；
- 确定性校验未通过（`verification_passed = False`）：$\text{VerificationConfidence} = 0 \implies \text{InfoGain} = 0$；
- **V3.1 预测误差校准**：`TaskFinalizer` 计算 `prediction_error = verified_ig - predicted_ig` 写回 Goal/Cycle 审计并反馈 `AutonomousLoop._last_prediction_error`，形成“预期 vs 现实”意外度闭环。

---

## 5. 动态 Self Model Beta-Binomial 后验（`SelfModelManager`）

以均匀先验 $\text{Beta}(\alpha_0=1.0, \beta_0=1.0)$ 为起点，依据 `capability_evidence` 中的实证成功/失败权重更新：

$$\alpha = \alpha_0 + W_{\text{succ}}, \quad \beta = \beta_0 + W_{\text{fail}}, \quad \hat{p} = \frac{\alpha}{\alpha + \beta}$$

通过正则化不完全 Beta 分布分位数函数计算 90% 单侧置信下界 $p_{\text{low}} = Q_{\text{Beta}}(0.10; \alpha, \beta)$；仅当 $N \ge \text{minimum\_samples}\,(3)$ 且 $p_{\text{low}} \ge 0.55$（或名下存在 SHA-256 完好的 `VALIDATED` Skill）时方可晋升 `VALIDATED`。

---

## 6. 五因子可解释执行路由（`AgentRouter`）

$$\text{RouteScore}(a) = \text{CapabilityMatch}(a) \times \text{EmpiricalReliability}(a) \times \text{Availability}(a) \times \text{CostEfficiency}(a) \times \text{RiskCompatibility}(a)$$

全候选分值与路由理由同步写入 `decision_logs(decision_type='execution_routing')`。

---

## 7. 薄内核预检、目标去重与外部探索引导（V3.0 – V4.0 M3）

1. **`ActionPreflight`（`app/cognition/preflight.py`，Fail-Closed）**：
   - 在外派 Agent 前零 LLM 校验：L0 `read-only` 与双语文件写意图（`detect_write_intent()` + `file_exists` 检查）冲突拦截、`VerificationSpec` 路径穿越拦截、Agent 可用性/权限匹配、超时与预算核对、近窗口重复 `plan_hash` 拦截。
2. **`GoalDedupEngine` 与 `compute_cooldown_until()`（`app/cognition/goal_dedup.py`）**：
   - 结构指纹：`sha256("<drive>|key:<related_key>" or "<drive>|tok:<top_canonical_tokens>")[:32]`；
   - 依次判定主动冷却期（`cooldown_until > now`）→ 窗口期精确/同键重复 → 语义余弦相似度（`≥ 0.86`）或词法 Jaccard 相似度（`≥ 0.72`）；
   - 失败任务按 `60m * min(streak, 4)` 退避，低 IG 成功（`< 0.15`）按 `120m` 退避，高 IG 成功设置 `15m` 防回声。
3. **多候选效用排序与外部好奇心纠偏（`select_best_goal_candidate()`）**：
   - 对主提案与最多 2 个备选目标中通过去重的候选计算：

$$\text{Utility} = \max\Big(0,\; 0.50\cdot\hat{\text{IG}} + 0.25\cdot\text{Novelty} + \text{Specificity} + \text{AffectFit} + \text{PrimaryBonus} + \text{ExternalBias}\Big)$$

   - 当 `ExplorationManager.build_guidance()` 检测到近期 Curiosity 目标中内部自检比例 $\ge \text{max\_internal\_curiosity\_ratio}\,(0.45)$ 时触发 `must_explore_external=True`，`ExternalBias` 对外部探索候选施加 `+0.25` 加分、对 `internal_self_check` 施加 `-0.30` 惩罚，确定性打破内部自检循环。

---

## 8. Skill-First Planning 与严格跨任务复用核算（V4.0 M1）

- **直接执行优先级**：规划产出 `ActionPlan.skill_invocation` 且目标技能为 `VALIDATED`、磁盘 SHA-256 未篡改、通过 Input/Output Schema 校验与 L0–L3 Governance 检查时，`AutonomousLoop._try_skill_first_execution()` 直接在隔离 `python -I` 沙箱执行 `SkillRuntime`，**消耗 0 Codex 调用与 0 Codex Token**；若未命中、权限受限或执行异常，则记录 `decision_type='skill_first_fallback'` 并无缝回退 Codex。
- **严格跨任务复用定义（`StateRepository.record_skill_invocation()`）**：
  - 仅在 `task_id != skill.source_task_id`（或 `source_task_id` 为空时存在不同 `task_id` / `cycle_id` 的历史 `passed=True` 调用）时记 `is_cross_task_reuse = True`；
  - 同任务反复自调用或“仅注入 Prompt 列表”绝不计入 `reuse_count`。仅当 `passed = True` 时累计保守估算节省 Token（默认 `40,000`）。

---

## 9. 有界 RSI 技能演进与语义去重（V4.0 M2 `SkillEvolutionEngine`）

1. **规范命名与语义去重**：`canonicalize_skill_name()` 将长句标题聚类映射为 `<domain>.<capability>.<operation>`；`find_duplicate_skill()` 基于规范名精确匹配或同能力域的“规范词法 Jaccard + 必需输入字段契约匹配（`+0.15`，联合阈值 `≥ 0.65`）”拦截重复造轮子并路由至演进分支。
2. **双门禁 + 客观改进守卫**：
   - **安全硬守卫**：禁止升级权限等级、禁止 L3、限制同族版本数（默认 `max_skill_versions_per_canonical = 10`）、AST 静态扫描；
   - **Gate A（回归测试）**：使用父版本原始 `test_spec` 在新 artifact 上运行 `python -I`，失败立即标记新版本 `DEPRECATED` 并记录 `skill.evolution_rolled_back`；
   - **Gate B（新边界测试）**：执行新 `test_code`（L2 需 `approved_l2=True`）；
   - **客观改进 & 延迟退化守卫**：必须满足（断言数增加 ∨ Schema 补齐 ∨ 延迟提速 ≥ 5% ∨ 父版本已有退化且新代码变化+有效变更摘要）且未发生延迟严重退化；晋升时将父版本 `superseded_by` 指向新版（`evolve_skill()` 自动继承父版本 `source_task_id` 保证跨任务复用溯源不断裂），`rollback_skill_version()` 可校验父版本 SHA-256 后一键还原。

---

## 10. 外置策略手册实证监控与自动回滚（V4.0 M3 `PlaybookManager`）

- **安全扫描**：保存前由 `scan_playbook_safety()` 结合 Prompt Injection 检测与内核/治理禁改规则扫描，确定性校验失败的周期也会在 `Brain` 与 `TaskFinalizer` 双重剥离 `playbook_update`；磁盘加载强校验 SHA-256。
- **退化自动回滚（`record_outcome_and_check_rollback()`）**：记录活跃版本的 `samples`、`successes` 与 `ig_sum`。当当前版本积累样本 $N \ge 3$（`rollback_min_samples`）且成功率 $r_{\text{cur}} < r_{\text{parent}} - 0.20$ 或（$r_{\text{cur}} < 0.50 \land r_{\text{parent}} \ge 0.70$，未采样父版本默认基准 `0.70`）时，自动校验父版本 Markdown 文件 SHA-256 并切回 `active_version = parent_version`。

---

## 11. 三层上下文压缩算法（V4.0 M4 `context_compactor.py`）

1. **Tier 1（核心必留）**：浓缩后的三大内驱力状态、健康度状态、配额预算与只读 `affect_state`。
2. **Tier 2（相关性 + 复用率重排 Top-K）**：`rank_and_compact_skills()` 按查询关键词重合度 $\text{Overlap} + \text{ReuseBonus}$（最高 `0.40`）排序后仅注入 Top-5 精简签名；`compact_playbooks()` 按领域提示优先并沿换行符安全截断至 `480` 字符（Top-2）。
3. **Tier 3（有界裁剪与围栏完整保障）**：`compact_memories()`、`compact_hypotheses()` 与 `compact_observation_dict()` 裁剪条数和预览字符；若截断发生在 `<untrusted_world_event ...>...</untrusted_world_event>` 文本内，**强制保留闭合结束标签**以防止外部数据越狱。
