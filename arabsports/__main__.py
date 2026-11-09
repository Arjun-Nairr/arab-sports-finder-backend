"""python -m arabsports run            # ingestion only: live discovery + news, no athlete argument
   python -m arabsports replay RUN_ID  # ingestion only: re-ingest a stored run's raw input (labelled replay, no network)
   python -m arabsports biographies    # bounded English Wikipedia biography import (verified sitelinks), background evidence
   python -m arabsports screen         # T03 on the stored library: packets -> local Laya -> one selection or none
   python -m arabsports angle FILE.json # record an independent angle review for the current selection (stores cited FIFA pages)
   python -m arabsports generate       # T04 on the latest selection + its approved angle: Groq composition -> package
   python -m arabsports revise RUN_ID FILE.json # save a manually revised copy of that run's draft as a new linked package
   python -m arabsports photos PACKAGE_ID FILE.json  # T06: bounded Commons photo lookup for that story, recorded as a run
   python -m arabsports export PACKAGE_ID [FILE.json] # T05: accepted package (+ its saved or a given presentation) -> showcase/site/data
   python -m arabsports preview PACKAGE_ID [FILE.json] # draft-labelled web preview + PDF of a package awaiting review -> showcase/review
   python -m arabsports pdf PACKAGE_ID  # five-page PDF of an exported (accepted) package -> showcase/exports
   python -m arabsports auto           # one automated attempt: discovery -> biographies -> Laya screen -> angle search (<= 5
                                       # ranked players, one call each) -> draft, check and save one package (no operator input)
   python -m arabsports batch          # as auto, then repeat on the same shortlist for up to 3 drafts about different
                                       # players (<= 5 players, <= 24 Groq calls); private previews of ready drafts
   python -m arabsports fixture-preview  # labelled layout fixture -> showcase/preview (never the deployable site)
   python -m arabsports review PACKAGE_ID accepted|rejected SHA256 --reviewer NAME --reason TEXT [--reason TEXT] [--resolves BLOCKER_ID]
                                       # record an independent review of that exact package version
   python -m arabsports workflow       # screen, then generate if something was selected (no discovery)"""
import sys

from .run import run


def main(argv):
    from . import stages
    if argv[:1] == ["run"] and len(argv) == 1:
        result = run("live")
    elif argv[:1] == ["replay"] and len(argv) == 2:
        result = run("replay", replay_of=argv[1])
    elif argv == ["biographies"]:
        result = stages.biography_run()
    elif argv[:1] == ["angle"] and len(argv) == 2:
        result = stages.angle_run(argv[1])
    elif argv[:1] == ["revise"] and len(argv) == 3:
        result = stages.revise_run(argv[1], argv[2])
    elif argv[:1] == ["photos"] and len(argv) == 3:
        result = stages.photo_run(argv[1], argv[2])
    elif argv[:1] in (["export"], ["preview"], ["pdf"]) and 2 <= len(argv) <= 3 - (argv[0] == "pdf"):
        from .export import ExportError
        fn = {"export": stages.export_run, "preview": stages.preview_run, "pdf": stages.pdf_run}[argv[0]]
        try:
            print(fn(*argv[1:]))
        except ExportError as e:
            print(f"{argv[0]} refused: {e}")
            return 1
        return 0
    elif argv in (["auto"], ["batch"]):
        prior = {}
        for name, step in (("discovery", lambda: run("live")), ("biographies", stages.biography_run),
                           ("screen", stages.screen_run)):
            r = step()
            print(f"{name} run {r['run_id']}: {r['outcome']}")
            prior[f"{name}_run"] = f"{r['run_id']} ({r['outcome']})"
            if r["outcome"] == "blocked":
                return 1
        # r: this screen run; its shortlist only
        result = (stages.auto_run if argv == ["auto"] else stages.batch_run)(r["run_id"], prior=prior)
    elif argv == ["fixture-preview"]:
        from . import export
        print(export.fixture_preview())
        return 0
    elif argv == ["screen"]:
        result = stages.screen_run()
    elif argv[:1] == ["review"] and len(argv) >= 4:
        import argparse
        ap = argparse.ArgumentParser(prog="python -m arabsports review")
        ap.add_argument("package_id"); ap.add_argument("decision", choices=["accepted", "rejected"]); ap.add_argument("sha")
        ap.add_argument("--reviewer", required=True); ap.add_argument("--reason", action="append", required=True)
        ap.add_argument("--resolves", action="append", default=[], help="acceptance blocker id explicitly resolved")
        a = ap.parse_args(argv[1:])
        from .store import ReviewError
        try:
            print(stages.review(a.package_id, a.decision, a.reviewer, a.reason, a.sha, resolves=a.resolves))
        except ReviewError as e:
            print(f"review refused: {e}")
            return 1
        return 0
    elif argv == ["generate"]:
        result = stages.generate_run()
    elif argv == ["workflow"]:
        result = stages.screen_run()
        print(f"run {result['run_id']}: {result['outcome']}\nreport: {result['report']}")
        if result["outcome"] != "selected":
            return 1
        result = stages.generate_run()
    else:
        print(__doc__)
        return 2
    print(f"run {result['run_id']}: {result['outcome']}\nreport: {result['report']}\ncounts: {result['counts']}")
    return 0 if result["outcome"] in ("completed", "completed_with_source_failures", "selected", "no_selection",
                                     "awaiting_review", "recorded", "no_suitable_photo",
                                     "candidate_shortlisted", "no_results", "no_supported_angle", "ready_target",
                                     "player_limit", "call_limit", "no_candidates") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
