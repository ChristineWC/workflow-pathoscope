from pathlib import Path
from types import SimpleNamespace
from typing import List

import aiofiles
import shlex
from virtool_workflow import fixture, step
from virtool_workflow.analysis.indexes import Index
from virtool_workflow.analysis.subtractions import Subtraction
from virtool_workflow.analysis.reads import Reads
from virtool_workflow.execution.run_subprocess import RunSubprocess


@fixture
def index(indexes: List[Index]):
    return indexes[0]


@fixture
def subtraction(subtractions: List[Subtraction]):
    return subtractions[0]


@fixture
def intermediate():
    """A namespace for storing intermediate values."""
    return SimpleNamespace(
        to_otus=set(),
        lengths=None,
    )


@fixture
def isolate_path(work_path: Path):
    path = work_path / "isolates"
    path.mkdir()
    return path


@fixture
def p_score_cutoff():
    return 0.01


@step
async def map_default_isolates(
        pathoscope,
        intermediate: dict,
        reads: Reads,
        index: Index,
        proc: int,
        p_score_cutoff: float,
        run_subprocess: RunSubprocess,
):
    """
    Map reads to the main OTU reference.

    This will be used to identify canididate OTUs.
    """

    async def stdout_handler(line: str):
        line = line.decode()

        if line[0] == "#" or line[0] == "@":
            return

        fields = line.split("\t")

        # Bitwise FLAG - 0x4: segment unmapped
        if int(fields[1]) & 0x4 == 4:
            return

        ref_id = fields[2]

        if ref_id == "*":
            return

        # Skip if the p_score does not meet the minimum cutoff.
        if pathoscope.find_sam_align_score(fields) < p_score_cutoff:
            return

        intermediate.to_otus.add(ref_id)

    await run_subprocess(
        [
            "bowtie2",
            "-p", str(proc),
            "--no-unal"
            "--local",
            "--score-min", "L,20,1.0",
            "-N", "0",
            "-L", "15",
            "-x", index.path,
            "-U", f"{reads.left},{reads.right}",
        ],
        wait=True,
        stdout_handler=stdout_handler
    )

    return f"Mapped reats to OTUs {intermediate.to_otus}"


@step
async def write_isolate_fasta(
    intermediate,
    index: Index,
    proc: int,
    isolate_path: Path,
):
    fasta_path = isolate_path/"isolate_index.fa",
    intermediate.lengths = await index.write_isolate_fasta(
        [index.get_otu_id_by_sequence_id(id_) for id_ in intermediate.to_otus],
        fasta_path,
        proc
    )

    intermediate.isolate_fasta_path = fasta_path

    return "Produced isolate fasta file."


@step
async def build_isolate_index(
    intermediate,
    isolate_path,
    run_subprocess: RunSubprocess,
    proc: int,
):
    """
    Build an index using the fasta file generated by :func:`.write_isolate_fasta`.
    """
    await run_subprocess(
        [
            "bowtie2-build",
            "--threads", str(proc),
            intermediate.isolate_fasta_path,
            isolate_path/"isolates",
        ],
        wait=True
    )

    return "Built isolate index."


@step
async def map_isolates(
    pathoscope,
    intermediate,
    reads: Reads,
    isolate_path: Path,
    run_subprocess: RunSubprocess,
    proc: int,
    p_score_cutoff: float,
    index: Index,
):
    """Map the sample reads to the newly built index."""
    vta_path = isolate_path/"to_isolates.vta"
    mapped_fastq_path = isolate_path/"mapped.fastq"
    reference_path = isolate_path/"isolates"
    async with aiofiles.open(vta_path, "w") as f:
        async def stdout_handler(line):
            line = line.decode()

            if line[0] == "@" or line == "#":
                return

            fields = line.split("\t")

            # Bitwise FLAG - 0x4 : segment unmapped
            if int(fields[1]) & 0x4 == 4:
                return

            ref_id = fields[2]

            if ref_id == "*":
                return

            p_score = pathoscope.find_sam_align_score(fields)

            # Skip if the p_score does not meet the minimum cutoff.
            if p_score < p_score_cutoff:
                return

            await f.write(",".join([
                fields[0],  # read_id
                ref_id,
                fields[3],  # pos
                str(len(fields[9])),  # length
                str(p_score)
            ]) + "\n")

        await run_subprocess(
            [
                "bowtie2",
                "-p", str(proc),
                "--no-unal",
                "--local",
                "--score-min", "L,20,1.0",
                "-N", "0",
                "-L", "15",
                "-k", "100",
                "--al", mapped_fastq_path,
                "-x", reference_path,
                "-U", f"{reads.left},{reads.right}"
            ],
            wait=True,
            stdout_handler=stdout_handler,
        )

    intermediate.isolate_vta_path = vta_path
    intermediate.isolate_mapped_fastq_path = mapped_fastq_path
    intermediate.isolate_bt2_path = reference_path

    return "Mapped sample reads to isolate index."


@step
async def map_subtractions(
    pathoscope,
    intermediate,
    subtraction: Subtraction,
    run_subprocess: RunSubprocess,
    proc: int,
):
    """
    Map the reads to the subtraction host for the sample.
    """

    to_subtraction = {}

    async def stdout_handler(line):
        line = line.decode()

        if line[0] == "@" or line == "#":
            return

        fields = line.split("\t")

        # Bitwise FLAG - 0x4 : segment unmapped
        if int(fields[1]) & 0x4 == 4:
            return

        # No ref_id assigned.
        if fields[2] == "*":
            return

        to_subtraction[fields[0]] = pathoscope.find_sam_align_score(fields)

    await run_subprocess(
        [
            "bowtie2",
            "--local",
            "-N", "0",
            "-p", str(proc),
            "-x", shlex.quote(str(subtraction.path)),
            "-U", intermediate.isolate_mapped_fastq_path
        ],
        wait=True,
        stdout_handler=stdout_handler,
    )

    intermediate.to_subtraction = to_subtraction

    return "Mapped reads to the subtraction host."


@step
async def subtract_mapping(
    pathoscope,
    intermediate,
    results,
    run_in_executor,
    isolate_path: Path,
):
    output_path = isolate_path/"subtracted.vta"

    subtracted_count = await pathoscope.subtract(
        intermediate.isolate_vta_path,
        output_path,
        intermediate.to_subtraction
    )

    await run_in_executor(
        pathoscope.replace_after_subtraction,
        output_path,
        intermediate.isolate_vta_path
    )

    results["subtracted_count"] = subtracted_count

    return "Performed subtraction on subtraction mapped reads."


@step
async def reassignment(
    pathoscope,
    intermediate,
    results,
    run_in_executor,
    isolate_path: Path,
    index: Index,
):
    """
    Run the Pathoscope reassignment algorithm.

    Tab-separated output is written to `pathoscope.tsv`.

    The results are also parsed and saved to `intermediate.coverage`.
    """
    reassigned_path = isolate_path / "reassigned.vta"
    (
        best_hit_initial_reads,
        best_hit_initial,
        level_1_initial,
        level_2_initial,
        best_hit_final_reads,
        best_hit_final,
        level_1_final,
        level_2_final,
        init_pi,
        pi,
        refs,
        reads
    ) = await run_in_executor(
        pathoscope.run_patho,
        intermediate.isolate_vta_path,
        reassigned_path,
    )

    read_count = len(reads)

    report_path = isolate_path/"report.tsv"
    report = await run_in_executor(
        pathoscope.write_report,
        report_path,
        pi,
        refs,
        read_count,
        init_pi,
        best_hit_initial,
        best_hit_initial_reads,
        best_hit_final,
        best_hit_final_reads,
        level_1_initial,
        level_2_initial,
        level_1_final,
        level_2_final
    )

    intermediate.coverage = run_in_executor(
        pathoscope.calculate_coverage,
        reassigned_path,
        intermediate.lengths
    )

    hits = []

    for sequence_id, hit in report.items():
        otu_id = index.get_otu_id_by_sequence_id(sequence_id)

        hit["id"] = sequence_id

        # Attach "otu" (id, version) to the hit.
        hit["otu"] = {
            "id": otu_id,
            "version": index.manifest[otu_id]
        }

        # Get the coverage for the sequence.
        hit_coverage = intermediate.coverage[sequence_id]

        # Calculate coverage and attach to hit.
        hit["coverage"] = round(
            1 - hit_coverage.count(0) / len(hit_coverage), 3)

        # Calculate depth and attach to hit.
        hit["depth"] = round(sum(hit_coverage) / len(hit_coverage))

        hits.append(hit)

    results.update({
        "ready": True,
        "read_count": read_count,
        "results": hits
    })
