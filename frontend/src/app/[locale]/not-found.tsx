import { Link } from "@/i18n/navigation";

export default function NotFound() {
  return (
    <main className="flex min-h-[60vh] items-center justify-center px-6">
      <div className="w-full max-w-lg rounded-lg border border-border bg-bg-surface p-6 font-mono">
        <div className="mb-4 flex items-center gap-2">
          <span className="h-3 w-3 rounded-full bg-red-500" />
          <span className="h-3 w-3 rounded-full bg-yellow-500" />
          <span className="h-3 w-3 rounded-full bg-green-500" />
          <span className="ml-2 text-sm text-text-muted">terminal</span>
        </div>
        <div className="space-y-2 text-sm">
          <p>
            <span className="text-emerald-400">$</span> cat page.html
          </p>
          <p className="text-red-400">
            cat: page.html: No such file or directory
          </p>
          <p>
            <span className="text-emerald-400">$</span> echo $?
          </p>
          <p className="text-6xl font-bold text-text-primary">404</p>
          <p className="text-text-muted">Page not found.</p>
          <p className="mt-4">
            <span className="text-emerald-400">$</span>{" "}
            <Link href="/" className="text-indigo-400 hover:underline">
              cd /home
            </Link>
          </p>
        </div>
      </div>
    </main>
  );
}
