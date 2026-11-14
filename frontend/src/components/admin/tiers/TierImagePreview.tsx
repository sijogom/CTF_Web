"use client";

import { useState } from "react";
import { Award, ImageOff } from "lucide-react";

interface TierImagePreviewProps {
  imageUrl: string;
  name: string;
  errorLabel: string;
}

export function TierImagePreview({ imageUrl, name, errorLabel }: TierImagePreviewProps) {
  const [failed, setFailed] = useState(false);

  return (
    <div className="mt-5 flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-bg-primary">
      {imageUrl && !failed ? (
        // Tier icons retain their existing uploaded/external URL contract.
        // eslint-disable-next-line @next/next/no-img-element
        <img src={imageUrl} alt={name} className="h-8 w-8 object-contain" onError={() => setFailed(true)} />
      ) : failed ? (
        <ImageOff className="h-5 w-5 text-text-muted" role="img" aria-label={errorLabel} />
      ) : (
        <Award className="h-5 w-5 text-text-muted" aria-hidden="true" />
      )}
    </div>
  );
}
