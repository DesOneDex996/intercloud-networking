import datetime
import itertools
import json
import logging
import os
import random
import shutil
import string
import sys
import threading
from typing import List, Dict, Tuple


from history.attempted import remove_already_attempted, write_attempted_tests
from cloud.clouds import Cloud, CloudRegion, interregion_distance
from history.results import combine_results_to_jsonl, untested_regionpairs, jsonl_to_csv
from util.subprocesses import run_subprocess
from util.utils import dedup

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)


def __env_for_singlecloud_subprocess(run_id, cloud_region):
    return {
        "PATH": os.environ["PATH"],
        "REGION": cloud_region.region_id,
        "RUN_ID": run_id,
    } | cloud_region.env()


def __create_vms(
    regions: List[CloudRegion], run_id: str
) -> List[Tuple[CloudRegion, Dict]]:
    # TODO Improve thread use with ThreadPoolExecutor and futures
    def create_vm(
        run_id_: str,
        cloud_region_: CloudRegion,
        vm_region_and_address_infos_inout: List[Tuple[CloudRegion, Dict]],
    ):
        logging.info("Will launch a VM in %s", cloud_region_)
        env = __env_for_singlecloud_subprocess(run_id_, cloud_region_)

        process_stdout = run_subprocess(cloud_region_.script(), env)
        vm_addresses = {}
        vm_address_info = process_stdout
        if vm_address_info[-1] == "\n":
            vm_address_info = vm_address_info[:-1]
        vm_address_infos = vm_address_info.split(",")
        vm_addresses["address"] = vm_address_infos[0]
        if len(vm_address_infos) > 1:
            vm_addresses["name"] = vm_address_infos[1]
            vm_addresses["zone"] = vm_address_infos[2]

        vm_region_and_address_infos_inout.append((cloud_region_, vm_addresses))

    def sort_addr_by_region(
        vm_region_and_address_infos: List[Tuple[CloudRegion, Dict]],
        regions: List[CloudRegion],
    ):
        ret = []
        for region in regions:
            for_this_region = [t for t in vm_region_and_address_infos if t[0] == region]

            if len(for_this_region) != 1:
                logging.error(
                    "For region %s found this data %s. Had these VMs %s}",
                    region,
                    for_this_region,
                    vm_region_and_address_infos,
                )
            if for_this_region:
                ret.append(for_this_region[0])
        return ret

    vm_region_and_address_infos = []
    threads = []
    regions_dedup = dedup(regions)
    for cloud_region in regions_dedup:
        thread = threading.Thread(
            name=f"create-{cloud_region}",
            target=create_vm,
            args=(run_id, cloud_region, vm_region_and_address_infos),
        )
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join()
        logging.info('create_vm in "%s" done', thread.name)

    ret = sort_addr_by_region(vm_region_and_address_infos, regions)
    return ret


def __do_tests(
    run_id: str,
    vm_region_and_address_infos: List[Tuple[CloudRegion, Dict]],
):
    results_dir_for_this_runid = f"./result-files-one-run/results-{run_id}"
    try:
        os.mkdir(results_dir_for_this_runid)
    except FileExistsError:
        pass

    def run_test(run_id, src: Tuple[CloudRegion, Dict], dst: Tuple[CloudRegion, Dict]):
        logging.info("running test from %s to %s", src, dst)
        src_region_, src_addr_infos = src
        dst_region_, dst_addr_infos = dst
        env = {
            "PATH": os.environ["PATH"],
            "RUN_ID": run_id,
            "SERVER_PUBLIC_ADDRESS": dst_addr_infos["address"],
            "SERVER_CLOUD": dst_region_.cloud.name,
            "CLIENT_CLOUD": src_region_.cloud.name,
            "SERVER_REGION": dst_region_.region_id,
            "CLIENT_REGION": src_region_.region_id,
        }
        if src_region.cloud == Cloud.AWS:
            env |= {
                "CLIENT_PUBLIC_ADDRESS": src_addr_infos["address"],
                "BASE_KEYNAME": "intercloudperfkey",
            }
        elif src_region.cloud == Cloud.GCP:
            try:
                env |= {
                    "CLIENT_NAME": src_addr_infos["name"],
                    "CLIENT_ZONE": src_addr_infos["zone"],
                }
            except KeyError as ke:
                logging.error("{src_addr_infos=}")
                raise ke

        else:
            raise Exception(f"Implement {src_region}")
        non_str = [(k, v) for k, v in env.items() if type(v) != str]
        assert not non_str, non_str

        script = src_region.script_for_test_from_region()
        process_stdout = run_subprocess(script, env)
        logging.info(
            "Test %s result from %s to %s is %s", run_id, src, dst, process_stdout
        )
        test_result = process_stdout + "\n"
        result_j = json.loads(test_result)
        result_j["distance"] = interregion_distance(src_region_, dst_region_)

        # We write separate files for each test to avoid race conditions, since tests happen in parallel.
        with open(
            f"{results_dir_for_this_runid}/results-{src_region_}-to-{dst_region_}.json",
            "w",
        ) as f:
            json.dump(result_j, f)

    vm_pairs: List[Tuple[Tuple[CloudRegion, Dict], Tuple[CloudRegion, Dict]]]

    assert len(vm_region_and_address_infos) % 2 == 0, (
        f"Must provide an even number of region in pairs for tests:"
        f" was length {len(vm_region_and_address_infos)}: {vm_region_and_address_infos}"
    )

    vm_pairs = [
        (vm_region_and_address_infos[i], vm_region_and_address_infos[i + 1])
        for i in range(0, len(vm_region_and_address_infos), 2)
    ]

    logging.info(
        "%s tests and %s regions ",
        len(vm_pairs),
        len(vm_region_and_address_infos),
    )
    threads = []

    for src, dest in vm_pairs:
        src_region = src[0]
        dst_region = dest[0]
        thread_name = f"{src_region}-{dst_region}"
        logging.info(f"Will run test %s", thread_name)
        thread = threading.Thread(
            name=thread_name, target=run_test, args=(run_id, src, dest)
        )
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join()
        logging.info('"%s" done', thread.name)

    combine_results_to_jsonl(results_dir_for_this_runid)
    #shutil.rmtree(results_dir_for_this_runid)


def __delete_vms(run_id, regions: List[CloudRegion]):
    def delete_aws_vm(aws_cloud_region: CloudRegion):
        assert aws_cloud_region.cloud == Cloud.AWS, aws_cloud_region
        logging.info(
            "Will delete EC2 VMs from run-id %s in %s", run_id, aws_cloud_region
        )
        env = __env_for_singlecloud_subprocess(run_id, aws_cloud_region)
        script = cloud_region.deletion_script()
        _ = run_subprocess(script, env)

    # First, AWS
    aws_regions = [r for r in regions if r.cloud == Cloud.AWS]
    threads = []

    for cloud_region in aws_regions:
        thread = threading.Thread(
            name=f"delete-{cloud_region}", target=delete_aws_vm, args=(cloud_region,)
        )
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join()
        logging.info("%s done", thread.name)

    # Now GCP

    gcp_regions = [r for r in regions if r.cloud == Cloud.GCP]

    if gcp_regions:
        cloud_region = gcp_regions[
            0
        ]  # One arbitrary region, for getting values for GCP.
        logging.info("Will delete GCE VMs from run-id %s", run_id)
        env = __env_for_singlecloud_subprocess(run_id, cloud_region)
        _ = run_subprocess(cloud_region.deletion_script(), env)


def __setup_and_tests_and_teardown(run_id: str, regions: List[CloudRegion]):
    """regions taken pairwise"""
    # Because we launch VMs and runs tests multithreaded, if one launch fails or one tests fails, run_tests() will not thrown an Exception.
    # So, VMs will still be cleaned up
    assert len(regions) % 2 == 0, f"Expect pairs {regions}"

    vm_region_and_address_infos = __create_vms(regions, run_id)
    logging.info(vm_region_and_address_infos)
    __do_tests(run_id, vm_region_and_address_infos)
    __delete_vms(run_id, regions)


def test_region_pairs(region_pairs: List[Tuple[CloudRegion, CloudRegion]], run_id):
    write_attempted_tests(region_pairs)
    regions = list(itertools.chain(*region_pairs))
    __setup_and_tests_and_teardown(run_id, regions)


def main():
    logging.info("Started at %s", datetime.datetime.now().isoformat())
    run_id = "".join(random.choices(string.ascii_lowercase, k=4))
    if len(sys.argv) > 1:
        gcp_project = sys.argv[1]
    else:
        gcp_project = None  # use default

    region_pairs = untested_regionpairs()
    region_pairs = remove_already_attempted(region_pairs)
    region_pairs.sort()

    group_size = 6
    groups_ = [
        region_pairs[i : i + group_size]
        for i in range(0, len(region_pairs), group_size)
    ]
    groups_ = groups_[:2]  # REMOVE!
    tot_len=sum(len(g) for g in groups_)
    logging.info(f"Running test on only {tot_len}")
    for group in groups_:
        test_region_pairs(group, run_id)
    jsonl_to_csv()


if __name__ == "__main__":
    main()
