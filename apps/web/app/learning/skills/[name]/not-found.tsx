import Link from "next/link";

export default function SkillNotFound() {
  return (
    <section aria-labelledby="skill-not-found-heading" className="space-y-3">
      <h1 id="skill-not-found-heading" className="text-2xl font-semibold">
        Skill not found
      </h1>
      <p className="max-w-prose text-slate-800">
        There is no skill with this name. It may have been mistyped, or the database may have been reset.
      </p>
      <p>
        <Link href="/learning/skills" className="text-blue-800 underline underline-offset-4">
          Back to skill versions
        </Link>
      </p>
    </section>
  );
}
