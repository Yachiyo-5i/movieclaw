import type { Metadata } from "next";

import { DiscoveredPersonDetailView } from "@/components/discovered-person-detail-view";

/** 人名需等客户端接口返回后由 usePageTitle 覆盖。 */
export const metadata: Metadata = { title: "TMDB 影人" };

/** 发现页专用人物路由：完整展示 TMDB 影视履历，不改变媒体库人物页语义。 */
export default async function DiscoveredPersonPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return (
    <div className="flex h-full flex-col">
      <DiscoveredPersonDetailView tmdbPersonId={id} />
    </div>
  );
}
