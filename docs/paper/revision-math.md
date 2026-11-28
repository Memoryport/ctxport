# 修订稿：公式与论述的严谨化（对应 migration-loss-paper-zh.pdf）

本文件只给出替换文本（LaTeX），对应 PDF 的节号与式号。拿到论文源码后逐条替换。

## 1. 式 (2) 之后：损失分解（§3「损失分解」段）

**问题**：原文称 $1-\rho$、$\rho(1-\beta)$、$\rho\beta(1-\upsilon)$ “恰好把所有错误划分开”。实际上三者之和是 $1-\rho\beta\upsilon = 1-\Pr[C]+\gamma$，比错误率多出 $\gamma$。

**替换为**：

> 对非拒答题，三个事件 $\neg E$、$E\wedge\neg K$、$K$ 两两不交且并为全集（由 $K\subseteq E$），因此
> $$\Pr[\neg C] \;=\; \underbrace{\Pr[\neg E\wedge\neg C]}_{\ell_{\text{ext}}} \;+\; \underbrace{\Pr[E\wedge\neg K\wedge\neg C]}_{\ell_{\text{bud}}} \;+\; \underbrace{\Pr[K\wedge\neg C]}_{\ell_{\text{read}}} \tag{2'}$$
> 这三项才恰好划分全部错误。它们与式 (2) 中各因子的关系为
> $$\ell_{\text{ext}} = (1-\rho) - \gamma_{\text{ext}},\qquad \ell_{\text{bud}} = \rho(1-\beta) - \gamma_{\text{bud}},\qquad \ell_{\text{read}} = \rho\beta(1-\upsilon),$$
> 其中 $\gamma_{\text{ext}}=\Pr[\neg E\wedge C]$，$\gamma_{\text{bud}}=\Pr[E\wedge\neg K\wedge C]$，$\gamma=\gamma_{\text{ext}}+\gamma_{\text{bud}}$。因此
> $$(1-\rho)+\rho(1-\beta)+\rho\beta(1-\upsilon) \;=\; \Pr[\neg C] + \gamma .$$
> 图 2 按 (2') 绘制，并单列 $\gamma_{\text{ext}}$ 与 $\gamma_{\text{bud}}$（“上下文无答案但答对”）。在所有有预算的方法上 $\gamma\le 0.9\%$，两种画法的差别不超过该值。

图 2 的数据本来就按事件计数（“上下文无答案但答对”是单独一栏），所以只需改正文措辞，必要时把该栏按 $\gamma_{\text{ext}}/\gamma_{\text{bud}}$ 拆开。

## 2. $K_q\subseteq E_q$ 的适用范围（§3，式 (2) 之前）

**问题**：原文写“对本文研究的方法有 $K_q\subseteq E_q$”。对 CHMD 成立（预算版是完整档案的行子集）；对一次性摘要、BM25、最近消息，图 2 中 $Z_\infty$ 实际取的是 $B=4000$ 的输出，而 $B=1400$ 的摘要可能写进 4000 版没写的事实，BM25 的 top-k 也不必嵌套，所以包含关系**不按构造成立**。

**替换为**（两种写法任选其一）：

- (a) 定义 $E_q=\{g(q)\sqsubseteq Z_\infty\}\cup\{g(q)\sqsubseteq Z\}$。于是 $K_q\subseteq E_q$ 恒成立，(2) 与 (2′) 对所有方法都是恒等式；代价是对非嵌套方法，$\rho$ 依赖于 $B$。
- (b) 保留原定义，并报告违反次数 $\#\{q: K_q\wedge\neg E_q\}$（脚本 `benchmarks/migration/analyze.py` 的 `nesting_violations` 会计算）；违反不为零的方法改用 (a)。

建议正文用 (a)，附录报告 (b) 的计数。

## 3. 命题 1（§3.1）

原证明正确，且实际上证明了更强的结论：在不含时间信息的上下文下，$\Pr[C_{q_{\text{cur}}}]\le\tfrac12$ 和 $\Pr[C_{q_{\text{prev}}}]\le\tfrac12$ **各自**成立，平均值的上界只是推论。建议写明以下前提：

> 设读者的回答分布只依赖于 $(Z,q)$，即 $a\sim\pi(\cdot\mid Z,q)$，问题文本 $q$ 本身不泄露先后顺序；且两个值的先后顺序先验等可能。则对任意（可随机化的）读者，$\Pr[C_{q_{\text{cur}}}]\le\tfrac12$ 且 $\Pr[C_{q_{\text{prev}}}]\le\tfrac12$。

另外，“定义 1”要求 $Z$ 同时包含 $v^{\text{old}}$ 和 $v^{\text{new}}$。若 $Z$ 只含其中一个，命题不适用（这时准确率由“保留了哪个值”决定）。§6.2 引用 124 道成对题时，应只统计两值都在 $Z$ 中的题，或者明确说明 17.7% 是对全部成对题的平均值，不能直接与上界比较。

## 4. 保留率模型 式 (6)（§4）

**问题**：$\beta_{\text{id}}(B)\approx\min(1,B/(n\bar\ell))$ 依赖未写明的假设。

**替换为**：

> 假设 (i) 被问到的事实与重要度 $w$ 独立；(ii) 行长 $\ell_c$ 与 $w$ 独立，且方差相对 $B$ 可忽略。则按 $w$ 贪心选取得到的行数约为 $B/\bar\ell$，于是
> $$\mathbb{E}[\beta_{\text{id}}(B)] \approx \min\!\Big(1,\frac{B}{n\bar\ell}\Big)=\min\!\Big(1,\frac{B}{|Z_\infty|}\Big).$$
> 若 (i) 不成立（例如被问的恰是高频事实），实际保留率会高于此式，此式就成为下界的近似。

§6.3 的预测值（0.48 / 0.78）与实测（0.44 / 0.81）之间的差距可以用 (ii) 的违反（跳过放不下的行，末尾留有空隙）来解释。

## 5. “无法区分”→ 等价检验（§6.4、摘要）

“CHMD 与一次性摘要在统计上无法区分”不等于二者等价。改为预先设定等价边界 $\delta$ 的 TOST：

> 以 $\delta=5$ 个百分点为等价边界，对人物级配对差做两个单侧检验（人物级 bootstrap 的 90% 区间完全落在 $(-\delta,\delta)$ 内即判等价）。

S 规模的 90% 区间若在 $(-5,5)$ 内可以写“等价”，否则只能写“未检出差异，也不能判定等价”。`analyze.py` 的 `tost` 会给出结论。

## 6. 摘要与引言中的措辞

- “把下游准确率精确分解为……三项之积” → “把下游准确率写成三项之积加一个猜中项 $\gamma$（恒等式）”。
- “主要损失所在的环节随规模变化” → 在 L 规模只有 4 个人物时，这只是方向一致的假设；扩大规模后（见 `benchmarks/migration/`）再改为结论。
