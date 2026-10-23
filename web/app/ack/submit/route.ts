/** Confirmation previews stay read-only; this endpoint requires an explicit same-origin POST. */
import { forwardControl } from "@/lib/control-backend";

export async function POST(request: Request) {
  return forwardControl(request, "/ack");
}
