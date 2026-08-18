export type Locale = "zh" | "en";

export const MESSAGES = {
  zh: {
    "app.name": "AISBench",
    "app.signInPrompt": "请登录后继续",
    "auth.createAccount": "创建账号",
    "auth.login": "登录",
    "auth.password": "密码",
    "auth.passwordHint": "至少 8 个字符",
    "auth.register": "注册",
    "auth.signOut": "退出登录",
    "auth.username": "用户名",
    "common.language": "English",
    "common.loading": "加载中",
    "nav.comparison": "对比分析",
    "nav.datasets": "共享数据集",
    "nav.jobs": "我的任务",
    "nav.models": "我的模型",
    "nav.newJob": "新建评测",
    "nav.recent": "最近任务",
  },
  en: {
    "app.name": "AISBench",
    "app.signInPrompt": "Sign in to continue",
    "auth.createAccount": "Create account",
    "auth.login": "Sign in",
    "auth.password": "Password",
    "auth.passwordHint": "At least 8 characters",
    "auth.register": "Register",
    "auth.signOut": "Sign out",
    "auth.username": "Username",
    "common.language": "中文",
    "common.loading": "Loading",
    "nav.comparison": "Comparison",
    "nav.datasets": "Shared Datasets",
    "nav.jobs": "My Jobs",
    "nav.models": "My Models",
    "nav.newJob": "New Evaluation",
    "nav.recent": "Recent jobs",
  },
} as const;

export type MessageKey = keyof (typeof MESSAGES)["zh"];
