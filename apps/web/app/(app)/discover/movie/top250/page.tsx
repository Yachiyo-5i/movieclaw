import { redirect } from "next/navigation";

/** 兼容旧书签；片单页面已统一使用稳定 collectionRef 路由。 */
export default function Top250Page() {
  redirect("/discover/movie/collections/douban/movie_top250");
}
