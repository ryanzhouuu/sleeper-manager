/** Private reads share the browser's owner cookie and are never cached. */
import { forwardControl } from "@/lib/control-backend";

export async function GET(request: Request) {
  return forwardControl(request, "/manage");
}
