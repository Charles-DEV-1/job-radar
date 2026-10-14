import { redirect } from "next/navigation";
import { requirePageSession } from "@/lib/auth";
export default async function Home() {
  await requirePageSession();
  redirect("/jobs");
}
