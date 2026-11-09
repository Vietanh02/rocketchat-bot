import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Skill, SkillDetail } from "../api/types";
import { useAuth } from "../auth";
import { Banner, Card, Checkbox, Empty, Loading, Pill } from "../components/ui";
import { errorMessage, useResource } from "../hooks";

export default function SkillsPage() {
  const { isAdmin } = useAuth();
  const skills = useResource(api.listSkills, []);
  const folder = useResource(api.skillsFolder, []);
  const [open, setOpen] = useState<SkillDetail | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const fail = (caught: unknown) => setNotice({ kind: "error", text: errorMessage(caught) });

  const toggle = (skill: Skill) =>
    void api
      .setSkill(skill.name, !skill.enabled)
      .then((next) => skills.setData((skills.data ?? []).map((s) => (s.name === next.name ? next : s))))
      .catch(fail);

  const show = (skill: Skill) =>
    open?.name === skill.name ? setOpen(null) : void api.getSkill(skill.name).then(setOpen).catch(fail);

  const list = skills.data ?? [];

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Skills</h1>
          <p>
            Instructions, and scripts, for kinds of tasks. The assistant sees each skill's name and
            description, reads its <code>SKILL.md</code> when a request matches, and runs its scripts with{" "}
            <code>skill_run</code>: by default only for trusted people (
            <Link to="/access">Access → Tools by profile</Link>).
          </p>
        </div>
        <div className="row">
          <button type="button" onClick={() => void skills.reload()}>
            Look again
          </button>
        </div>
      </div>

      {notice && <Banner kind={notice.kind}>{notice.text}</Banner>}

      <Card title="Skills folder">
        {folder.data && (
          <p style={{ margin: 0 }}>
            <code>{folder.data.path}</code>{" "}
            {folder.data.exists ? <Pill kind="ok">found</Pill> : <Pill kind="warn">missing</Pill>}
          </p>
        )}
        <p className="hint">
          One folder per skill, holding a <code>SKILL.md</code> (front matter: <code>name</code>,{" "}
          <code>description</code>, optional <code>requires.bins</code> and <code>requires.env</code>), an
          optional <code>scripts/</code> folder and any files it mentions. In Docker this is{" "}
          <code>skills/</code> next to <code>docker-compose.yml</code>, read-only; see{" "}
          <code>skills/_example</code>. Folders starting with <code>_</code> are skipped.
        </p>
      </Card>

      <Card title="Found">
        {skills.loading && !skills.data ? (
          <Loading />
        ) : skills.error ? (
          <Banner kind="error">{skills.error}</Banner>
        ) : list.length === 0 ? (
          <Empty>No skills in the folder.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Skill</th>
                  <th>State</th>
                  <th>Scripts</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {list.map((skill) => [
                  <tr key={skill.name}>
                    <td>
                      <strong>{skill.name}</strong>
                      <div className="hint">{skill.description || "—"}</div>
                    </td>
                    <td>
                      {skill.available ? (
                        <Pill kind="ok">offered</Pill>
                      ) : !skill.enabled ? (
                        <Pill>off</Pill>
                      ) : (
                        <Pill kind="warn">not usable</Pill>
                      )}
                      {skill.error && <div className="hint">{skill.error}</div>}
                      {skill.missing.map((why) => (
                        <div className="hint" key={why}>
                          {why}
                        </div>
                      ))}
                    </td>
                    <td className="muted">{skill.scripts.length ? skill.scripts.join(", ") : "—"}</td>
                    <td>
                      <div className="row">
                        <button type="button" onClick={() => show(skill)}>
                          {open?.name === skill.name ? "Hide" : "SKILL.md"}
                        </button>
                        <Checkbox
                          label="On"
                          checked={skill.enabled}
                          disabled={!isAdmin}
                          onChange={() => toggle(skill)}
                        />
                      </div>
                    </td>
                  </tr>,
                  open?.name === skill.name && (
                    <tr key={`${skill.name}-text`}>
                      <td colSpan={4}>
                        <pre>{open.text}</pre>
                        {open.files.length > 0 && <div className="hint">Files: {open.files.join(", ")}</div>}
                      </td>
                    </tr>
                  ),
                ])}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
