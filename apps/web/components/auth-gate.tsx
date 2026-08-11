"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import type { Route } from "next";
import { usePathname, useRouter } from "next/navigation";

import { getBootstrapStatus, getSession, type SessionView } from "@/lib/api/auth";
import { HttpError, resolveRequestUrl } from "@/lib/http";
import { SessionProvider } from "@/lib/session";
import { accessiblePathFor } from "@/lib/permissions";

interface StartupFailure {
  endpoint: string;
  message: string;
}

/**
 * 首页的鉴权门：在确认登录状态之前不渲染工作台，消除
 * "先闪一下主界面、再跳转登录/引导页"的割裂体验。
 *
 * 判定顺序（都是一次极轻的 GET）：
 * 1. 系统未初始化 → 直接去 /setup（不再经 /login 二连跳）；
 * 2. 已初始化但未登录 → getSession 得到 401，由 http.ts 统一跳 /login；
 * 3. 已登录 → 把会话数据注入 SessionProvider，放行渲染工作台。
 * 等待与失败状态使用不依赖业务 Provider 的轻量面板，API 不可达时仍可重试和自检。
 */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [session, setSession] = useState<SessionView | null>(null);
  const [failure, setFailure] = useState<StartupFailure | null>(null);
  const [retryKey, setRetryKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let endpoint = "/auth/bootstrap";
    setFailure(null);
    (async () => {
      try {
        const status = await getBootstrapStatus();
        if (cancelled) return;
        if (!status.initialized) {
          router.replace("/setup");
          return;
        }
        endpoint = "/auth/me";
        const view = await getSession(); // 未登录时抛 401，http.ts 拦截并整页跳 /login
        if (cancelled) return;
        const allowedPath = accessiblePathFor(view, pathname);
        if (allowedPath !== pathname) {
          router.replace(allowedPath as Route);
          return;
        }
        setSession(view);
      } catch (error) {
        if (cancelled) return;
        // 401 已由 http.ts 发起整页跳转；此处继续保持启动态，避免跳转前闪出错误卡。
        if (error instanceof HttpError && error.status === 401) return;
        const message = startupFailureMessage(error);
        console.error(`工作台启动失败（${resolveRequestUrl(endpoint)}）：`, error);
        setFailure({ endpoint, message });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pathname, retryKey, router]);

  if (!session) {
    return (
      <StartupStatus
        failure={failure}
        onRetry={() => setRetryKey((value) => value + 1)}
      />
    );
  }
  return <SessionProvider value={{ session, setSession }}>{children}</SessionProvider>;
}

/** 把启动异常收敛成部署用户能理解的中文信息；后端主动返回的业务消息原样保留。 */
function startupFailureMessage(error: unknown): string {
  if (!(error instanceof HttpError)) {
    return "无法连接到服务端，请检查 MovieClaw 是否正常运行以及当前访问地址。";
  }
  if (error.message === `Request failed with status ${error.status}`) {
    return `服务端请求失败（HTTP ${error.status}），请检查服务状态或反向代理配置。`;
  }
  return error.message;
}

/**
 * 工作台启动状态使用纯 CSS 面板，不依赖 AppShell、用户偏好接口或 WebGL。
 * 这样即使故障正发生在这些初始化环节，用户仍能看到原因并自行重试。
 */
function StartupStatus({
  failure,
  onRetry,
}: {
  failure: StartupFailure | null;
  onRetry: () => void;
}) {
  return (
    <main className="viewport-app-height relative z-10 flex w-full items-center justify-center p-6">
      <section
        role={failure ? "alert" : "status"}
        aria-live="polite"
        className="w-full max-w-[420px] rounded-2xl border border-white/[0.1] bg-[rgba(13,15,21,0.88)] p-6 shadow-2xl backdrop-blur-xl"
      >
        <p className="text-sub font-semibold uppercase tracking-[0.18em] text-[var(--text-faint)]">
          MovieClaw
        </p>
        {failure ? (
          <>
            <h1 className="mt-2 text-title font-semibold text-[var(--text)]">工作台加载失败</h1>
            <p className="mt-2 text-ui leading-relaxed text-[var(--text-muted)]">
              {failure.message}
            </p>
            <p className="mt-3 break-all rounded-lg bg-black/25 px-3 py-2 font-mono text-caption text-[var(--text-faint)]">
              请求：{resolveRequestUrl(failure.endpoint)}
            </p>
            <div className="mt-5 flex flex-wrap gap-3">
              <button
                type="button"
                onClick={onRetry}
                className="btn-accent h-9 rounded-full px-5 text-ui font-semibold"
              >
                重新连接
              </button>
              <Link
                href="/health"
                className="btn-glass flex h-9 items-center rounded-full px-5 text-ui font-medium"
              >
                查看系统状态
              </Link>
            </div>
          </>
        ) : (
          <div className="mt-3 flex items-center gap-3 text-ui text-[var(--text-muted)]">
            <span
              className="size-4 shrink-0 animate-spin rounded-full border-2 border-white/20 border-t-white/70"
              aria-hidden="true"
            />
            正在连接服务…
          </div>
        )}
      </section>
    </main>
  );
}
