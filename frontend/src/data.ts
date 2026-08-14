// 生产前端样例的 Mock 数据与类型定义。
// 说明：本文件仅用于设计样例（prototype.html），字段形态参考未来 API 契约，不与现有 demo 代码耦合。

export type PageKey =
  | "home"
  | "chat"
  | "plan"
  | "map"
  | "notebook"
  | "community"
  | "profile";

export interface User {
  name: string;
  initials: string;
  streakDays: number;
  joinedDays: number;
}

export interface PlanTask {
  id: string;
  title: string;
  kind: "学" | "练" | "复习";
  topic: string;
  done: boolean;
  minutes: number;
}

export interface PlanDay {
  weekday: string;
  date: string;
  isToday: boolean;
  tasks: PlanTask[];
}

export interface Conversation {
  id: string;
  title: string;
  time: string;
  preview: string;
  active?: boolean;
}

export interface Source {
  book: string;
  chapter: string;
  page: number;
  snippet: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  markdown?: string;
  text?: string;
  sources?: Source[];
}

export interface KnowledgeNode {
  id: string;
  name: string;
  domain: string;
  mastery: number; // 0 - 1
  x: number; // SVG 坐标
  y: number;
  linkTo: string[];
}

export interface NoteItem {
  id: string;
  question: string;
  answerExcerpt: string;
  tags: string[];
  source: string;
  addedAt: string;
  nextReview: string;
  reviewStage: number; // 间隔重复第几轮
  mastery: "薄弱" | "巩固中" | "已掌握";
}

export interface StudyGroup {
  id: string;
  name: string;
  members: number;
  desc: string;
  todayActive: number;
  joined?: boolean;
}

export interface Notification {
  id: string;
  kind: "plan" | "community" | "review";
  text: string;
  time: string;
  read: boolean;
}

export interface MemoryItem {
  category: "掌握度" | "学习偏好" | "目标";
  text: string;
  updatedAt: string;
}

export const user: User = {
  name: "林舟",
  initials: "舟",
  streakDays: 12,
  joinedDays: 46,
};

export const planMeta = {
  goal: "六周建立线性代数直觉",
  progress: 0.58,
  weekLabel: "第 4 周 · 特征值与对角化",
  createdAt: "7 月 6 日",
};

export const todayTasks: PlanTask[] = [
  { id: "t1", title: "理解特征值与特征向量的定义", kind: "学", topic: "特征值", done: true, minutes: 25 },
  { id: "t2", title: "推导特征多项式 |A − λI| = 0", kind: "学", topic: "特征多项式", done: true, minutes: 20 },
  { id: "t3", title: "向 AI 提问：对角化的几何意义", kind: "练", topic: "对角化", done: false, minutes: 15 },
  { id: "t4", title: "复习：矩阵的秩（3 条错题到期）", kind: "复习", topic: "矩阵的秩", done: false, minutes: 10 },
];

export const weekPlan: PlanDay[] = [
  {
    weekday: "周一", date: "8/3", isToday: false,
    tasks: [
      { id: "w1", title: "相似矩阵的定义与性质", kind: "学", topic: "相似矩阵", done: true, minutes: 30 },
      { id: "w2", title: "复习：行列式展开", kind: "复习", topic: "行列式", done: true, minutes: 10 },
    ],
  },
  {
    weekday: "周二", date: "8/4", isToday: true,
    tasks: todayTasks,
  },
  {
    weekday: "周三", date: "8/5", isToday: false,
    tasks: [
      { id: "w3", title: "对角化的充要条件", kind: "学", topic: "对角化", done: false, minutes: 30 },
      { id: "w4", title: "向 AI 提问：不可对角化的矩阵长什么样", kind: "练", topic: "对角化", done: false, minutes: 15 },
    ],
  },
  {
    weekday: "周四", date: "8/6", isToday: false,
    tasks: [
      { id: "w5", title: "实对称矩阵的正交对角化", kind: "学", topic: "实对称矩阵", done: false, minutes: 35 },
    ],
  },
  {
    weekday: "周五", date: "8/7", isToday: false,
    tasks: [
      { id: "w6", title: "本周小结：向 AI 复述特征值全链路", kind: "练", topic: "综合", done: false, minutes: 20 },
      { id: "w7", title: "复习到期错题", kind: "复习", topic: "综合", done: false, minutes: 15 },
    ],
  },
];

export const conversations: Conversation[] = [
  { id: "c1", title: "特征值与特征向量", time: "10:24", preview: "几何上，特征向量是变换下方向不变的直线……", active: true },
  { id: "c2", title: "矩阵的秩为什么重要", time: "昨天", preview: "秩衡量的是列空间的真实维度……" },
  { id: "c3", title: "泰勒展开的余项怎么理解", time: "周一", preview: "拉格朗日余项给出了误差的精确上界……" },
  { id: "c4", title: "条件概率与贝叶斯", time: "上周", preview: "先验被证据更新后得到后验……" },
];

export const chatThread: ChatMessage[] = [
  {
    id: "m1",
    role: "user",
    text: "特征值和特征向量到底是什么？书上定义 Av = λv 我看了很多遍还是不理解。",
  },
  {
    id: "m2",
    role: "assistant",
    markdown: [
      "好问题——先忘掉公式，想象一个**线性变换**（比如把平面旋转、拉伸）。大多数向量在变换后方向都会改变，但总有一些特殊的方向，变换只把它们**拉长或缩短**，不旋转它们。",
      "",
      "这些特殊方向上的向量就是**特征向量**，拉长/缩短的倍数就是**特征值**：",
      "",
      "$$A\\mathbf{v} = \\lambda\\mathbf{v}$$",
      "",
      "这句话读作：矩阵 $A$ 作用在 $\\mathbf{v}$ 上，效果等同于一个标量 $\\lambda$ 乘以 $\\mathbf{v}$——**复杂变换在这个方向上退化为简单缩放**。",
      "",
      "## 一个具体例子",
      "",
      "取 $A = \\begin{pmatrix} 2 & 1 \\\\ 1 & 2 \\end{pmatrix}$，解特征方程 $\\det(A - \\lambda I) = 0$：",
      "",
      "$$(2-\\lambda)^2 - 1 = 0 \\implies \\lambda_1 = 3,\\ \\lambda_2 = 1$$",
      "",
      "- $\\lambda_1 = 3$ 对应特征向量 $(1,1)$：沿对角线方向的向量被放大 3 倍；",
      "- $\\lambda_2 = 1$ 对应特征向量 $(1,-1)$：沿反对角线方向的向量保持不动。",
      "",
      "所以特征值分解的本质是：**找到变换的「主轴」，把复杂变换拆解成各主轴上的独立缩放**。这也是对角化、谱定理、PCA 的共同思想源头。",
    ].join("\n"),
    sources: [
      {
        book: "《线性代数（同济第七版）》",
        chapter: "第五章 · 相似矩阵及二次型",
        page: 118,
        snippet: "设 A 是 n 阶矩阵，如果数 λ 和 n 维非零列向量 x 使关系式 Ax = λx 成立，那么这样的数 λ 称为矩阵 A 的特征值……",
      },
      {
        book: "《Linear Algebra Done Right (4th)》",
        chapter: "Chapter 5 · Eigenvalues and Eigenvectors",
        page: 134,
        snippet: "An eigenvector of an operator is a nonzero vector whose direction is unchanged (up to scaling) by the operator.",
      },
    ],
  },
];

export const followups = [
  "特征值的几何意义能画图解释吗？",
  "为什么实对称矩阵一定可以正交对角化？",
  "特征值和行列式、迹有什么关系？",
];

export const knowledgeNodes: KnowledgeNode[] = [
  { id: "k1", name: "行列式", domain: "线性代数", mastery: 0.9, x: 130, y: 90, linkTo: ["k2", "k4"] },
  { id: "k2", name: "矩阵运算", domain: "线性代数", mastery: 0.85, x: 300, y: 60, linkTo: ["k3", "k4"] },
  { id: "k3", name: "向量空间", domain: "线性代数", mastery: 0.7, x: 470, y: 100, linkTo: ["k5"] },
  { id: "k4", name: "矩阵的秩", domain: "线性代数", mastery: 0.35, x: 220, y: 210, linkTo: ["k5"] },
  { id: "k5", name: "特征值", domain: "线性代数", mastery: 0.55, x: 400, y: 230, linkTo: ["k6"] },
  { id: "k6", name: "对角化", domain: "线性代数", mastery: 0.2, x: 560, y: 290, linkTo: [] },
  { id: "k7", name: "极限", domain: "微积分", mastery: 0.95, x: 90, y: 330, linkTo: ["k8"] },
  { id: "k8", name: "导数", domain: "微积分", mastery: 0.88, x: 200, y: 410, linkTo: ["k9"] },
  { id: "k9", name: "泰勒展开", domain: "微积分", mastery: 0.6, x: 340, y: 460, linkTo: [] },
  { id: "k10", name: "条件概率", domain: "概率论", mastery: 0.5, x: 520, y: 420, linkTo: ["k11"] },
  { id: "k11", name: "贝叶斯定理", domain: "概率论", mastery: 0.3, x: 620, y: 500, linkTo: [] },
];

export const domains = ["线性代数", "微积分", "概率论"];

export const notes: NoteItem[] = [
  {
    id: "n1",
    question: "为什么矩阵的秩等于行阶梯形的非零行数？",
    answerExcerpt: "初等行变换保持行空间不变，而行阶梯形的非零行线性无关，构成行空间的一组基……",
    tags: ["矩阵的秩", "线性代数"],
    source: "对话「矩阵的秩为什么重要」",
    addedAt: "8 月 2 日",
    nextReview: "今天",
    reviewStage: 2,
    mastery: "薄弱",
  },
  {
    id: "n2",
    question: "rank(AB) ≤ min(rank A, rank B) 的直观解释？",
    answerExcerpt: "AB 的列空间包含于 A 的列空间，复合变换不会让信息维度增加……",
    tags: ["矩阵的秩"],
    source: "对话「矩阵的秩为什么重要」",
    addedAt: "8 月 2 日",
    nextReview: "今天",
    reviewStage: 2,
    mastery: "薄弱",
  },
  {
    id: "n3",
    question: "可逆矩阵的等价条件有哪些？",
    answerExcerpt: "满秩、行列式非零、零空间只有零向量、特征值全部非零……共十条等价刻画。",
    tags: ["可逆矩阵", "线性代数"],
    source: "对话「矩阵的秩为什么重要」",
    addedAt: "8 月 1 日",
    nextReview: "今天",
    reviewStage: 1,
    mastery: "巩固中",
  },
  {
    id: "n4",
    question: "泰勒公式的拉格朗日余项怎么估计误差？",
    answerExcerpt: "余项 Rₙ(x) = f⁽ⁿ⁺¹⁾(ξ)/(n+1)! · (x−x₀)ⁿ⁺¹，用导数上界即可控制误差……",
    tags: ["泰勒展开", "微积分"],
    source: "对话「泰勒展开的余项怎么理解」",
    addedAt: "7 月 28 日",
    nextReview: "8 月 6 日",
    reviewStage: 3,
    mastery: "巩固中",
  },
  {
    id: "n5",
    question: "贝叶斯公式里先验和后验的角色？",
    answerExcerpt: "先验是看到证据前的信念 P(H)，证据 E 通过似然 P(E|H) 把它更新为后验 P(H|E)……",
    tags: ["贝叶斯", "概率论"],
    source: "对话「条件概率与贝叶斯」",
    addedAt: "7 月 25 日",
    nextReview: "8 月 9 日",
    reviewStage: 4,
    mastery: "已掌握",
  },
];

export const studyGroups: StudyGroup[] = [
  { id: "g1", name: "线代攻坚小队", members: 18, desc: "六周刷完线性代数核心章节，每周日复盘", todayActive: 11, joined: true },
  { id: "g2", name: "微积分晨读会", members: 32, desc: "每天早上 30 分钟，共读《普林斯顿微积分读本》", todayActive: 9 },
  { id: "g3", name: "概率论互助组", members: 15, desc: "从条件概率到马尔可夫链，互相讲题", todayActive: 6 },
];

export const checkin = {
  // 8 月日历：今天是 8/4（下标 3），连续 12 天打卡的尾部落在 8/1–8/4，未来日期不可打卡。
  monthDays: Array.from({ length: 31 }, (_, i) => i <= 3),
  leaderboard: [
    { name: "艾宾浩斯本斯", days: 31 },
    { name: "正方形骑士", days: 28 },
    { name: "林舟", days: 12, me: true },
    { name: "秩不平", days: 9 },
  ],
};

export const notifications: Notification[] = [
  { id: "nf1", kind: "review", text: "3 条「矩阵的秩」错题今天到期复习", time: "09:00", read: false },
  { id: "nf2", kind: "community", text: "正方形骑士 回复了你的帖子「特征值的直觉」", time: "10:12", read: false },
  { id: "nf3", kind: "plan", text: "今日任务还剩 2 项：对角化提问、错题复习", time: "12:30", read: false },
  { id: "nf4", kind: "community", text: "「线代攻坚小队」今日已有 11 人打卡", time: "昨天", read: true },
];

export const memories: MemoryItem[] = [
  { category: "目标", text: "六周内建立线性代数的整体直觉，重点是特征值与对角化", updatedAt: "7 月 6 日" },
  { category: "掌握度", text: "行列式、矩阵运算较扎实；矩阵的秩的几何理解薄弱", updatedAt: "8 月 2 日" },
  { category: "掌握度", text: "微积分基础牢固（极限/导数），泰勒展开误差估计在巩固中", updatedAt: "7 月 28 日" },
  { category: "学习偏好", text: "偏好先直觉后定义：先用几何例子，再给严格表述", updatedAt: "7 月 20 日" },
  { category: "学习偏好", text: "晚上 20:00–22:00 学习，单次约 25–40 分钟", updatedAt: "7 月 18 日" },
];

export const weekMinutes = [35, 50, 20, 65, 40, 55, 0]; // 近 7 天学习分钟数
