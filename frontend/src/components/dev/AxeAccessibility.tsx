"use client";

import { useEffect } from "react";

export function AxeAccessibility() {
  useEffect(() => {
    if (process.env.NODE_ENV === "development") {
      import("@axe-core/react")
        .then((axe) =>
          Promise.all([import("react"), import("react-dom")]).then(
            ([React, ReactDOM]) => {
              axe.default(React, ReactDOM, 1000);
            },
          ),
        )
        .catch(() => {
          // React 19+ 호환성 문제 — axe-core가 createElement를 덮어쓰려 할 때 발생
        });
    }
  }, []);

  return null;
}
