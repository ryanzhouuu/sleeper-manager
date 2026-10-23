/** Shared status treatment keeps loading feedback readable with reduced motion. */
export function ControlStatus({ children }: { children: React.ReactNode }) {
  return (
    <p className="control-loading" role="status">
      <span className="control-spinner" aria-hidden="true" />
      {children}
    </p>
  );
}

export function ControlNotice({
  title,
  message,
}: {
  title: string;
  message?: string;
}) {
  return (
    <section className="block control-notice" role="status">
      <h1 className="label">{title}</h1>
      {message ? <p>{message}</p> : null}
    </section>
  );
}
