"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import type { Placement } from "@floating-ui/react";

import { Tooltip } from "@/components/tooltip";

/**
 * 自动截断并按需显示完整内容的通用文字组件。
 *
 * Tooltip 只在元素实际溢出时启用，避免未截断文字制造重复提示；截断后元素
 * 才进入键盘焦点顺序，移动端也可点击打开。ResizeObserver 同时覆盖响应式
 * 宽度、侧栏伸缩和字体加载后的重新排版。
 */
export function OverflowText({
  children,
  tooltipContent,
  className = "",
  lines = 1,
  placement = "top",
  maxWidth = 520,
  alwaysShowTooltip = false,
  focusable = true,
}: {
  children: ReactNode;
  /** 默认复用可见内容；需要去掉样式或补充上下文时可单独传入。 */
  tooltipContent?: ReactNode;
  className?: string;
  lines?: number;
  placement?: Placement;
  maxWidth?: number;
  /** 即使文字未溢出也显示提示，适用于提示内容比可见文字更精确的场景。 */
  alwaysShowTooltip?: boolean;
  /** 放在 button 等交互控件内部时关闭，避免产生嵌套焦点。 */
  focusable?: boolean;
}) {
  const textRef = useRef<HTMLSpanElement | null>(null);
  const [overflowing, setOverflowing] = useState(false);

  const measure = useCallback(() => {
    const element = textRef.current;
    if (!element) return;
    const next =
      lines === 1
        ? element.scrollWidth > element.clientWidth + 1
        : element.scrollHeight > element.clientHeight + 1;
    setOverflowing((current) => (current === next ? current : next));
  }, [lines]);

  useEffect(() => {
    measure();
    const element = textRef.current;
    if (!element) return;
    const observer =
      typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(element);
    window.addEventListener("resize", measure);
    let active = true;
    void document.fonts?.ready.then(() => {
      if (active) measure();
    });
    return () => {
      active = false;
      observer?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [children, measure]);

  const tooltipEnabled = alwaysShowTooltip || overflowing;
  const text = (
    <span
      ref={textRef}
      tabIndex={tooltipEnabled && focusable ? 0 : undefined}
      className={`block min-w-0 max-w-full ${
        lines === 1
          ? "truncate"
          : "overflow-hidden [display:-webkit-box] [-webkit-box-orient:vertical]"
      } ${className}`}
      style={lines === 1 ? undefined : { WebkitLineClamp: Math.max(1, lines) }}
    >
      {children}
    </span>
  );

  if (!tooltipEnabled) return text;
  return (
    <Tooltip
      content={
        tooltipContent ?? (
          <span className="whitespace-pre-wrap break-words [overflow-wrap:anywhere]">
            {children}
          </span>
        )
      }
      placement={placement}
      maxWidth={maxWidth}
      openOnClick
    >
      {text}
    </Tooltip>
  );
}
