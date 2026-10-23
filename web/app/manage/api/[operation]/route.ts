/** Only known owner mutations can be forwarded to the Worker. */
import { forwardControl } from "@/lib/control-backend";
import type { ControlOperation } from "@/lib/control-types";

const OPERATIONS: readonly string[] = ["login", "save", "replan", "logout"];

export async function POST(
  request: Request,
  { params }: { params: Promise<{ operation: string }> },
) {
  const { operation } = await params;
  if (!OPERATIONS.includes(operation))
    return new Response(null, { status: 404 });
  return forwardControl(request, `/manage/${operation as ControlOperation}`);
}
