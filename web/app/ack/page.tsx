/** Validate the scoped notification URL without using cookies or recording a decision. */
import { loadControl } from "@/lib/control-backend";
import { ConfirmationScreen } from "@/components/confirmation-screen";

export const dynamic = "force-dynamic";

export default async function AckPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { token, action } = await searchParams;
  if (
    typeof token !== "string" ||
    !token ||
    token.length > 256 ||
    (action !== "locked" && action !== "passed")
  )
    return (
      <ConfirmationScreen
        initial={{
          status: "message",
          title: "Invalid confirmation",
          message: "Use the link in your notification.",
        }}
      />
    );
  const document = await loadControl(
    "/ack",
    "",
    new URLSearchParams({ token, action }).toString(),
  );
  return <ConfirmationScreen initial={document} />;
}
