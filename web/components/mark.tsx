"use client";

import Image from "next/image";
import { useState } from "react";

export function Mark({
  src,
  alt,
  size,
}: {
  src: string | null;
  alt: string;
  size: 16 | 20;
}) {
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  if (!src || failedSrc === src) {
    return null;
  }
  return (
    <Image
      className="mark"
      src={src}
      alt={alt}
      width={size}
      height={size}
      onError={() => setFailedSrc(src)}
    />
  );
}
