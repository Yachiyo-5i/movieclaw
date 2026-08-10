"use client";

/**
 * 设置 → 成员：家庭成员账号与权限管理（docs/design/member-management.md §3.9.1）。
 *
 * 结构为「列表 → 行内展开详情」两层：
 * - 列表行：头像 + 昵称（用户名）、状态徽标、能力摘要、最近登录时间；
 * - 展开后：能力开关（按依赖关系联动置灰）、可见库 / 可用站点白名单、
 *   重置密码 / 启停 / 删除。
 *
 * 权限写入口唯一在本页（人视角）；库设置页零改动。这里只是界面，
 * 安全边界在后端的 require_admin 与守护测试。
 */

import { useCallback, useEffect, useState } from "react";

import { AvatarBadge } from "@/components/avatar-badge";
import { useConfirm, useToast } from "@/components/feedback";
import { CheckIcon, PlusIcon } from "@/components/icons";
import { listLibraries, type MediaLibrary } from "@/lib/api/libraries";
import {
  createMember,
  deleteMember,
  listMembers,
  resetMemberPassword,
  setMemberStatus,
  updateMember,
  type MemberUpdatePayload,
  type MemberView,
} from "@/lib/api/members";
import { listSiteCatalog, type CatalogItem } from "@/lib/api/sites";
import { formatRelativeTime } from "@/lib/time";

/** 生成随机初始密码（前端生成便于「一键复制发给家人」，后端只存哈希）。 */
function generatePassword(): string {
  const alphabet = "abcdefghjkmnpqrstuvwxyzACDEFGHJKLMNPQRSTUVWXYZ2345679";
  const bytes = crypto.getRandomValues(new Uint8Array(14));
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("");
}

export function MembersSection() {
  const toast = useToast();
  const [members, setMembers] = useState<MemberView[] | null>(null);
  const [libraries, setLibraries] = useState<MediaLibrary[]>([]);
  const [sites, setSites] = useState<CatalogItem[]>([]);
  const [creating, setCreating] = useState(false);
  const [expanded, setExpanded] = useState<number | null>(null);

  const reload = useCallback(async () => {
    setMembers(await listMembers());
  }, []);

  useEffect(() => {
    void reload().catch((e) => toast.error(`加载成员失败：${(e as Error).message}`));
    // 白名单选择器的数据源：库列表 + 站点注册表（失败不阻塞成员列表本身）
    void listLibraries().then(setLibraries).catch(() => {});
    void listSiteCatalog().then(setSites).catch(() => {});
  }, [reload, toast]);

  return (
    <div className="space-y-8">
      <section>
        <div className="mb-2.5 flex items-center justify-between px-1">
          <h3 className="group-label">成员列表</h3>
          <button
            type="button"
            onClick={() => setCreating((v) => !v)}
            className="btn-glass px-3 py-1.5 text-sub font-medium"
          >
            <PlusIcon className="size-4" />
            <span>添加成员</span>
          </button>
        </div>

        {creating && (
          <CreateMemberCard
            onDone={(created) => {
              setCreating(false);
              if (created) void reload();
            }}
          />
        )}

        <div className="css-glass divide-y divide-white/[0.055] !rounded-2xl">
          {members == null ? (
            <p className="px-5 py-4 text-body text-[var(--text-faint)]">加载中…</p>
          ) : members.length === 0 ? (
            <p className="px-5 py-4 text-body text-[var(--text-faint)]">
              还没有成员。点右上角「添加成员」为家人朋友开通账号——他们只能浏览、
              播放与订阅，碰不到任何系统设置。
            </p>
          ) : (
            members.map((m) => (
              <MemberRow
                key={m.id}
                member={m}
                libraries={libraries}
                sites={sites}
                expanded={expanded === m.id}
                onToggle={() => setExpanded(expanded === m.id ? null : m.id)}
                onChanged={(next) =>
                  setMembers((rows) =>
                    rows == null ? rows : rows.map((r) => (r.id === next.id ? next : r)),
                  )
                }
                onRemoved={() => {
                  setExpanded(null);
                  void reload();
                }}
              />
            ))
          )}
        </div>
      </section>
    </div>
  );
}

/** 新建成员卡片：用户名 + 随机初始密码（可改）+ 昵称。 */
function CreateMemberCard({ onDone }: { onDone: (created: boolean) => void }) {
  const toast = useToast();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState(() => generatePassword());
  const [nickname, setNickname] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (username.trim().length < 3) {
      toast.error("用户名至少 3 个字符");
      return;
    }
    if (password.length < 8) {
      toast.error("密码至少 8 位");
      return;
    }
    setBusy(true);
    try {
      await createMember(username.trim(), password, nickname.trim());
      try {
        await navigator.clipboard.writeText(`账号：${username.trim()}  密码：${password}`);
        toast.success("成员已创建，账号密码已复制，直接粘贴发给对方");
      } catch {
        toast.success("成员已创建，请把账号密码发给对方");
      }
      onDone(true);
    } catch (e) {
      toast.error(`创建失败：${(e as Error).message}`);
      setBusy(false);
    }
  };

  return (
    <div className="css-glass mb-3 space-y-3 !rounded-2xl p-5">
      <div className="grid grid-cols-2 gap-3 max-md:grid-cols-1">
        <label className="block">
          <span className="mb-1 block text-caption text-[var(--text-muted)]">用户名（登录用）</span>
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="如 mama"
            className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
            autoFocus
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-caption text-[var(--text-muted)]">昵称（可选）</span>
          <input
            value={nickname}
            onChange={(e) => setNickname(e.target.value)}
            placeholder="如 老妈"
            className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
          />
        </label>
      </div>
      <label className="block">
        <span className="mb-1 block text-caption text-[var(--text-muted)]">
          初始密码（已随机生成，可修改；对方登录后可自行改密）
        </span>
        <div className="flex items-center gap-2">
          <input
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60 font-mono"
          />
          <button
            type="button"
            onClick={() => setPassword(generatePassword())}
            className="btn-glass shrink-0 px-3 py-1.5 text-sub font-medium"
          >
            换一个
          </button>
        </div>
      </label>
      <div className="flex items-center justify-end gap-2 pt-1">
        <button
          type="button"
          onClick={() => onDone(false)}
          className="btn-glass px-3.5 py-1.5 text-sub font-medium"
        >
          取消
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => void submit()}
          className="btn-glass px-3.5 py-1.5 text-sub font-semibold text-[var(--accent)] disabled:opacity-40"
        >
          <CheckIcon className="size-4" />
          <span>{busy ? "创建中…" : "创建成员"}</span>
        </button>
      </div>
    </div>
  );
}

/** 成员行：摘要 + 展开后的完整编辑面板。 */
function MemberRow({
  member,
  libraries,
  sites,
  expanded,
  onToggle,
  onChanged,
  onRemoved,
}: {
  member: MemberView;
  libraries: MediaLibrary[];
  sites: CatalogItem[];
  expanded: boolean;
  onToggle: () => void;
  onChanged: (next: MemberView) => void;
  onRemoved: () => void;
}) {
  const toast = useToast();
  const confirm = useConfirm();
  const disabled = member.status !== "active";

  const summary = [
    member.allow_subscribe ? "可订阅" : "不可订阅",
    member.allow_search
      ? member.all_sites
        ? "可搜索"
        : `可搜索(${member.site_ids.length} 站)`
      : "不可搜索",
    member.all_libraries ? "全部库" : `可见 ${member.library_ids.length} 库`,
  ].join(" · ");

  /** 局部更新并同步行数据；失败弹中文错误。 */
  const patch = async (payload: MemberUpdatePayload) => {
    try {
      onChanged(await updateMember(member.id, payload));
    } catch (e) {
      toast.error(`保存失败：${(e as Error).message}`);
    }
  };

  const resetPassword = async () => {
    const ok = await confirm({
      title: `重置「${member.nickname}」的密码？`,
      description: "会生成一个新的随机密码（旧密码与其所有登录立即失效），新密码只显示这一次。",
      confirmLabel: "重置密码",
    });
    if (!ok) return;
    try {
      const result = await resetMemberPassword(member.id);
      try {
        await navigator.clipboard.writeText(`账号：${result.username}  新密码：${result.password}`);
        toast.success(`新密码：${result.password}（已复制，请发给对方）`);
      } catch {
        toast.success(`新密码：${result.password}（请立即记下，不会再显示）`);
      }
    } catch (e) {
      toast.error(`重置失败：${(e as Error).message}`);
    }
  };

  const toggleStatus = async () => {
    if (!disabled) {
      const ok = await confirm({
        title: `停用「${member.nickname}」？`,
        description: "对方所有设备立即下线、无法再登录；数据全部保留，可随时重新启用。",
        confirmLabel: "停用",
        tone: "danger",
      });
      if (!ok) return;
    }
    try {
      onChanged(await setMemberStatus(member.id, disabled));
    } catch (e) {
      toast.error(`操作失败：${(e as Error).message}`);
    }
  };

  const remove = async () => {
    const ok = await confirm({
      title: `删除成员「${member.nickname}」？`,
      description:
        "其个人数据（头像、播放进度等）将被清理；其发起的订阅会转由管理员接管继续追更，已下载内容不受影响。此操作不可恢复。",
      confirmLabel: "删除成员",
      tone: "danger",
    });
    if (!ok) return;
    try {
      await deleteMember(member.id);
      toast.success("成员已删除");
      onRemoved();
    } catch (e) {
      toast.error(`删除失败：${(e as Error).message}`);
    }
  };

  return (
    <div className="first:rounded-t-2xl last:rounded-b-2xl">
      {/* 摘要行（点击展开/收起编辑面板） */}
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="flex w-full items-center gap-3.5 px-5 py-4 text-left"
      >
        <AvatarBadge
          nickname={member.nickname}
          avatarUrl={member.avatar_url}
          className="size-10"
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
            <span className="text-body font-medium text-[var(--text)]">{member.nickname}</span>
            <span className="text-caption text-[var(--text-faint)]">@{member.username}</span>
            {disabled && (
              <span className="rounded-full border border-white/[0.12] px-2 py-px text-caption font-medium text-[var(--danger)]">
                已停用
              </span>
            )}
          </div>
          <p className="mt-0.5 truncate text-caption text-[var(--text-muted)]">
            {summary}
            <span className="text-[var(--text-faint)]">
            {" · "}
            {member.last_login_at
              ? `最近登录 ${formatRelativeTime(member.last_login_at)}`
              : "从未登录"}
            </span>
          </p>
        </div>
        <span className="shrink-0 text-caption text-[var(--text-faint)]">
          {expanded ? "收起" : "管理"}
        </span>
      </button>

      {expanded && (
        <div className="space-y-5 border-t border-white/[0.055] px-5 pb-5 pt-4">
          {/* 能力开关 */}
          <div className="space-y-2.5">
            <ToggleRow
              label="允许订阅"
              hint="自助点播：发起订阅、管理自己的订阅（消耗盘空间与下载带宽）"
              checked={member.allow_subscribe}
              onChange={(v) => void patch({ allow_subscribe: v })}
            />
            <ToggleRow
              label="允许站点搜索"
              hint="聚合搜索 PT 站点资源（消耗站点配额、能看到接入了哪些站点）"
              checked={member.allow_search}
              onChange={(v) => void patch({ allow_search: v })}
            />
            <ToggleRow
              label="允许一键下载"
              hint="搜索结果直接下载（绕过订阅规则过滤；始终走自动选库，不暴露路径）"
              checked={member.allow_direct_download}
              disabled={!member.allow_search}
              onChange={(v) => void patch({ allow_direct_download: v })}
            />
          </div>

          {/* 可见库 */}
          <WhitelistPicker
            label="可见媒体库"
            allLabel="全部库（含以后新建的）"
            allSelected={member.all_libraries}
            options={libraries.map((l) => ({ key: String(l.id), label: l.name }))}
            selected={member.library_ids.map(String)}
            onChangeAll={(all) => void patch({ all_libraries: all })}
            onChangeSelected={(keys) =>
              void patch({ all_libraries: false, library_ids: keys.map(Number) })
            }
          />

          {/* 可用站点（仅开了搜索才有意义） */}
          {member.allow_search && (
            <WhitelistPicker
              label="可用站点"
              allLabel="全部启用中的站点"
              allSelected={member.all_sites}
              options={sites.map((s) => ({ key: s.site_id, label: s.display_name }))}
              selected={member.site_ids}
              onChangeAll={(all) => void patch({ all_sites: all })}
              onChangeSelected={(keys) => void patch({ all_sites: false, site_ids: keys })}
            />
          )}

          {/* 生命周期动作 */}
          <div className="flex flex-wrap items-center gap-2 border-t border-white/[0.055] pt-4">
            <button
              type="button"
              onClick={() => void resetPassword()}
              className="btn-glass px-3 py-1.5 text-sub font-medium"
            >
              重置密码
            </button>
            <button
              type="button"
              onClick={() => void toggleStatus()}
              className="btn-glass px-3 py-1.5 text-sub font-medium"
            >
              {disabled ? "重新启用" : "停用"}
            </button>
            <div className="flex-1" />
            <button
              type="button"
              onClick={() => void remove()}
              className="btn-glass px-3 py-1.5 text-sub font-medium !text-[var(--danger)]"
            >
              删除成员
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/** 开关行：标签 + 说明 + 原生 checkbox（与 webhook 设置的开关同款）。 */
function ToggleRow({
  label,
  hint,
  checked,
  disabled = false,
  onChange,
}: {
  label: string;
  hint: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label
      className={`flex items-center justify-between gap-4 ${disabled ? "opacity-45" : "cursor-pointer"}`}
    >
      <span className="min-w-0">
        <span className="block text-body font-medium text-[var(--text)]">{label}</span>
        <span className="mt-0.5 block text-caption text-[var(--text-faint)]">{hint}</span>
      </span>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="size-4 shrink-0 accent-[var(--accent)]"
      />
    </label>
  );
}

/**
 * 白名单选择器：单选「全部 / 仅选中的」，后者展开多选胶囊。
 * 库与站点共用同一形态（三件套范式在 UI 上的投影）。
 */
function WhitelistPicker({
  label,
  allLabel,
  allSelected,
  options,
  selected,
  onChangeAll,
  onChangeSelected,
}: {
  label: string;
  allLabel: string;
  allSelected: boolean;
  options: { key: string; label: string }[];
  selected: string[];
  onChangeAll: (all: boolean) => void;
  onChangeSelected: (keys: string[]) => void;
}) {
  const toggle = (key: string) => {
    const next = selected.includes(key)
      ? selected.filter((k) => k !== key)
      : [...selected, key];
    onChangeSelected(next);
  };

  return (
    <div>
      <p className="mb-1.5 text-body font-medium text-[var(--text)]">{label}</p>
      <div className="flex flex-wrap items-center gap-2">
        <Chip active={allSelected} onClick={() => onChangeAll(true)}>
          {allLabel}
        </Chip>
        <Chip active={!allSelected} onClick={() => onChangeAll(false)}>
          仅选中的
        </Chip>
      </div>
      {!allSelected && (
        <div className="mt-2 flex flex-wrap gap-2">
          {options.length === 0 ? (
            <p className="text-caption text-[var(--text-faint)]">暂无可选项</p>
          ) : (
            options.map((opt) => (
              <Chip key={opt.key} active={selected.includes(opt.key)} onClick={() => toggle(opt.key)}>
                {opt.label}
              </Chip>
            ))
          )}
        </div>
      )}
    </div>
  );
}

/** 选择胶囊：选中态高亮为主题色，未选中为暗玻璃描边。 */
function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-full border px-3 py-1 text-sub font-medium transition-colors ${
        active
          ? "border-[var(--accent)]/60 bg-[var(--accent-soft)] text-[var(--accent)]"
          : "border-white/[0.1] bg-white/[0.04] text-[var(--text-muted)] hover:text-[var(--text)]"
      }`}
    >
      {children}
    </button>
  );
}
