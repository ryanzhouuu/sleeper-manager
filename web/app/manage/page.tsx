/** Owner evidence is fetched per request with the app-origin session cookie. */
import { cookies } from "next/headers";
import { loadControl } from "@/lib/control-backend";
import { ManagerScreen } from "@/components/manager-screen";

export const dynamic = "force-dynamic";

export default async function ManagePage() {
  const session = (await cookies()).get("manager_session");
  const document = await loadControl(
    "/manage",
    session ? `manager_session=${session.value}` : "",
  );
  return <ManagerScreen initial={document} />;
}
