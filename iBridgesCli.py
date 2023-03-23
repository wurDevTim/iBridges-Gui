#!/usr/bin/env python3

"""
Commandline client to upload data to a storage service and double-link the storage location with a metadata store.

Implemented for:
    Storage types:
        iRODS
    Metadata stores:
        Elabjournal
"""

from utils.elabConnector import elabConnector
import irodsConnector.keywords as kw
from irodsConnector.manager import IrodsConnector
from irodsConnector.resource import FreeSpaceNotSet
from irods.exception import ResourceDoesNotExist, NoResultFound

import argparse
import logging
import configparser
import os
import sys
import json
import getopt
import getpass
from pathlib import Path
from utils.utils import setup_logger, get_local_size, ensure_dir

log_colors = {
    0: '\x1b[0m',    # DEFAULT (info)
    1: '\x1b[1;33m', # YEL (warn)
    2: '\x1b[1;31m', # RED (error)
    3: '\x1b[1;34m', # BLUE (success)
}

def print_error(msg):
    """
    Adds color code for error to message and calls logging.error.
    """
    logging.error("%s%s%s", log_colors[2], msg, log_colors[0])

def print_warning(msg):
    """
    Adds color code for warning to message and calls logging.warning.
    """
    logging.warning("%s%s%s", log_colors[1], msg, log_colors[0])

def print_success(msg):
    """
    Adds color code for success to message and calls logging.info.
    """
    logging.info("%s%s%s", log_colors[1], msg, log_colors[0])

def print_message(msg):
    """
    Calls logging.info.
    """
    logging.info(msg)


class iBridgesCli:                          # pylint: disable=too-many-instance-attributes
    """
    Class for up- and downloading to YODA/iRODS via the command line.
    Includes option for writing metadata to Elab Journal.
    """
    def __init__(self,                      # pylint: disable=too-many-arguments
                 config_file: str,
                 local_path: str,
                 irods_path: str,
                 irods_env: str,
                 irods_resc: str,
                 operation: str,
                 logdir: str,
                 skip_eln: bool) -> None:

        self.irods_env = None
        self.irods_path = None
        self.irods_resc = None
        self.local_path = None
        self.config_file = None

        # reading optional config file
        if config_file:
            if not os.path.exists(config_file):
                self._clean_exit(f"{config_file} does not exist")

            self.config_file = os.path.expanduser(config_file)
            if not self.get_config('iRODS'):
                self._clean_exit(f"{config_file} misses iRODS section")

        # CLI parameters override config-file
        self.irods_env = irods_env or self.get_config('iRODS', 'irodsenv') \
                         or self._clean_exit("need iRODS environment file", True)
        self.irods_path = irods_path or self.get_config('iRODS', 'irodscoll') \
                          or self._clean_exit("need iRODS path", True)
        self.local_path = local_path or self.get_config('LOCAL', 'path') \
                          or self._clean_exit("need local path", True)

        self.irods_env = Path(os.path.expanduser(self.irods_env))
        self.local_path = Path(os.path.expanduser(self.local_path))
        self.irods_path = self.irods_path.rstrip("/")
        logdir = Path(logdir)

        # checking if paths actually exist
        for path in [self.irods_env, self.local_path, logdir]:
            if not path.exists():
                self._clean_exit(f"{path} does not exist")

        # reading default irods_resc from env file if not specified otherwise
        self.irods_resc = irods_resc or self.get_config('iRODS', 'irodsresc') or None
        if not self.irods_resc:
            with open(self.irods_env,'r',encoding='utf-8') as file:
                cfg = json.load(file)
                if 'default_resource_name' in cfg:
                    self.irods_resc = cfg['default_resource_name']

        if not self.irods_resc:
            self._clean_exit("need an iRODS resource", True)

        self.operation = operation
        self.skip_eln = skip_eln
        setup_logger(logdir, "iBridgesCli")
        self._run()

    def _clean_exit(self, message=None, show_help=False, exit_code=1):
        if message:
            print_error(message)
        if show_help:
            iBridgesCli.parser.print_help()
        if self.irods_conn:
            self.irods_conn.cleanup()
        sys.exit(exit_code)

    def get_config(self, section: str, option: str=None):
        """
        Reads config file.
        """
        if not self.config_file:
            return False

        config = configparser.ConfigParser()
        with open(self.config_file, encoding='utf-8') as file:
            config.read_file(file)

            if section not in config.sections():
                return False

            if not option:
                return dict(config.items(section))

            if option in [x[0] for x in config.items(section)]:
                return config.get(section, option)

        return False

    @classmethod
    def from_arguments(cls):
        cls.parser = argparse.ArgumentParser(
            prog='python iBridgesCli.py',
            description="",
            epilog=""
            )

        default_logdir = os.path.join(str(os.getenv('HOME')), '.irods')
        default_irods_env = os.path.join(str(os.getenv('HOME')), '.irods', 'irods_environment.json')

        cls.parser.add_argument('--config', '-c',
                            type=str,
                            help='Config file')
        cls.parser.add_argument('--local_path', '-l',
                            help='Local path to download to, or upload from',
                            type=str)
        cls.parser.add_argument('--irods_path', '-i',
                            help='iRods path to upload to, or download from',
                            type=str)
        cls.parser.add_argument('--operation', '-o',
                            type=str,
                            choices=['upload', 'download'],
                            required=True)
        cls.parser.add_argument('--env', '-e', type=str,
                            help=f'iRods environment file. (example: {default_irods_env})')
        cls.parser.add_argument('--irods_resc', '-r', type=str,
                            help='iRods resource. If omitted default will be read from iRods env file.')
        cls.parser.add_argument('--logdir', type=str,
                            help=f'Directory for logfile. Default: {default_logdir}',
                            default=default_logdir)
        cls.parser.add_argument('--skip_eln', action="store_true",
                            help='Skip writing to ELN')

        args = cls.parser.parse_args()

        return cls(config_file=args.config,
                   irods_env=args.env,
                   irods_resc=args.irods_resc,
                   local_path=args.local_path,
                   irods_path=args.irods_path,
                   operation=args.operation,
                   logdir=args.logdir,
                   skip_eln=args.skip_eln,
                   )

    @classmethod
    def connect_irods(cls, irods_env):
        attempts = 0
        while True:
            secret = getpass.getpass(f'Password for {irods_env} (leave empty to use cached): ')
            try:
                irods_conn = IrodsConnector(irods_env, secret)
                # irods_conn.session.pool.get_connection()
                _ = irods_conn.server_version
                break
            except Exception as exception:
                print_error(f"AUTHENTICATION failed. {repr(exception)}")
                attempts += 1
                if attempts >= 3 or input('Try again (Y/N): ') not in ['Y', 'y']:
                    return False

        return irods_conn

    @classmethod
    def download(cls, irods_conn, source, target_folder):
        # checks if remote object exists and if it's an object or a collection
        if irods_conn.collection_exists(source):
            item = irods_conn.get_collection(source)
        elif irods_conn.dataobject_exists(source):
            item = irods_conn.get_dataobject(source)
        else:
            print_error(f'iRODS path {source} does not exist')
            return False

        # get its size to check if there's enough space
        download_size = irods_conn.get_irods_size([source])
        print_message(f"Downloading '{source}' (approx. {round(download_size * kw.MULTIPLIER, 2)}GB)")

        # download
        irods_conn.download_data(src_obj=item, dst_path=target_folder, size=download_size, force=False)

        print_success('Download complete')
        return True

    @classmethod
    def upload(cls, irods_conn, irods_resc, source, target_path):
        # check if intended upload target exists
        try:
            irods_conn.ensure_coll(target_path)
            print_warning(f"Uploading to {target_path}")
        except Exception:
            print_error(f"Collection path invalid: {target_path}")
            return False

        # check if there's enough space left on the resource
        upload_size = get_local_size([source])
        # TODO
        # free_space = int(irods_conn.get_free_space(resc_name=irods_resc))
        # print(free_space)
        free_space = None
        if free_space is not None and free_space-1000**3 < upload_size:
            print_error('Not enough space left on iRODS resource to upload.')
            return False

        irods_conn.upload_data(
            src_path=source,
            dst_coll=irods_conn.get_collection(target_path),
            resc_name=irods_resc,
            size=upload_size,
            force=True)

        return True

    @classmethod
    def setup_elab(cls, config):
        elab = elabConnector(config['token'])

        if 'group' in config and 'experiment' in config \
            and len(config['group']) > 0 and len(config['experiment']) > 0:
            try:
                elab.updateMetadataUrl(group=config['group'], experiment=config['experiment'])
            except Exception:
                print_error(f"ELN groupID {config['group']} or experimentID {config['experiment']} not set or valid.")
                elab.showGroups()
                elab.updateMetadataUrlInteractive(group=True)
        else:
            elab.showGroups()
            elab.updateMetadataUrlInteractive(group=True)

        # TODO: while loop to ensure title? or is it not mandatory? or lose interactivity altogether
        if not 'title' in config or len(config['title']) == 0:
            title = input('ELN paragraph title: ')
        else:
            title = config['title']

        print_message('Link Data to experiment: ')
        print_message(elab.metadataUrl)
        print_message(f'with title: {title}')

        return elab, title, elab.group.index[0], elab.experiment.id()

    @classmethod
    def annotate_elab(cls, irods_conn, elab, source, target_path, title='Data in iRODS'):  # pylint: disable=too-many-arguments
        coll = irods_conn.get_collection(target_path)

        annotation = {
            "iRODS path": coll.path,
            "iRODS server": irods_conn.host,
            "iRODS user": irods_conn.username,
        }

        # YODA: webdav URL does not contain "home", but iRODS path does!
        if irods_conn.davrods and ("yoda" in irods_conn.host or "uu.nl" in irods_conn.host):
            url = f"{irods_conn.davrods}/{coll.path.split('home/')[1].strip()}"
        elif irods_conn.davrods and "surfsara.nl" in irods_conn.host:
            url = f"{irods_conn.davrods}/{coll.path.split(irods_conn.zone)[1].strip('/')}"
        elif irods_conn.davrods:
            url = f"{irods_conn.davrods}/{coll.path.strip('/')}"
        else:
            url = '{' + "\n".join([irods_conn.host, irods_conn.zone,
                                   irods_conn.username, str(irods_conn.port), coll.path]) + '}'

        elab.addMetadata(url=url, meta=annotation, title=title)

        if os.path.isfile(source):
            item = irods_conn.get_dataobject(f"{coll.path}/{os.path.basename(source)}")
            irods_conn.add_metadata([item], 'ELN', elab.metadataUrl)
        elif os.path.isdir(source):
            uploaded_coll = irods_conn.get_collection(f"{coll.path}/{os.path.basename(source)}")
            items = [uploaded_coll]
            for this_coll, _, objs in uploaded_coll.walk():
                items.append(this_coll)
                items.extend(objs)
            irods_conn.add_metadata(items, 'ELN', elab.metadataUrl)






def printHelp():
    print('Data upload client')
    print('Uploads local data to iRODS, and, if specified, links dat to an entry in a metadata store (ELN).')
    print('Usage: ./iBridgesCli.py -c, --config= \t config file')
    print('\t\t    -d, --data= \t datapath')
    print('\t\t    -i, --irods= \t irodspath (download)')
    print('Examples:')
    print('Downloading: ./iBridgesCli.py -c <yourConfigFile> --irods=/npecZone/home')
    print('Uploading: ./iBridgesCli.py -c <yourConfigFile> --data=/my/data/path')


def main(argv):

    irodsEnvPath = os.path.expanduser('~') + os.sep + ".irods"
    setup_logger(irodsEnvPath, "iBridgesCli")

    try:
        opts, args = getopt.getopt(argv, "hc:d:i:", ["config=", "data=", "irods="])
    except getopt.GetoptError:
        print(RED+"ERROR: incorrect usage."+DEFAULT)
        printHelp()
        sys.exit(2)

    config = None
    operation = None

    for opt, arg in opts:
        if opt == '-h':
            printHelp()
            sys.exit(2)
        elif opt in ['-c', '--config']:
            try:
                config = getConfig(arg)
            except Exception:
                print(RED+'No config file found.'+DEFAULT)
                sys.exit(2)
        elif opt in ['-i', '--irods']:
            operation = 'download'
            if arg.endswith("/"):
                irodsPath = arg[:-1]
            else:
                irodsPath = arg
        elif opt in ['-d', '--data']:
            operation = 'upload'
            if arg.endswith("/"):
                dataPath = arg[:-1]
            else:
                dataPath = arg
        else:
            printHelp()
            sys.exit(2)

    # initialise iRODS
    if operation is None:
        print(RED+"ERROR: missing parameter."+DEFAULT)
        printHelp()
        sys.exit(2)
    ic = setupIRODS(config, operation)
    # initialise medata store connetcions
    if 'ELN' in config and operation == 'upload':
        md, title = setupELN(config)
    else:
        md = None

    # check files for upload
    if operation == 'upload':
        if len(config) == 1:
            print(BLUE+"INFO: No metadata store configured. Only upload data to iRODS."+DEFAULT)
        if prepareUpload(dataPath, ic, config):
            if md is not None:
                iPath = config['iRODS']['irodscoll'] + '/' + md.__name__ + '/' + \
                    str(config['ELN']['group']) + '/'+str(config['ELN']['experiment'])
            # elif os.path.isdir(dataPath):
            #     iPath = config['iRODS']['irodscoll']+'/'+os.path.basename(dataPath)
            else:
                iPath = config['iRODS']['irodscoll']
            iColl = ic.get_collection(iPath)
            dataPath = config["iRODS"]["uploadItem"]
            ic.upload_data(dataPath, iColl, config['iRODS']['irodsresc'],
                           get_local_size([dataPath]), force=True)
        else:
            ic.cleanup()
            sys.exit(2)
        # tag data in iRODS and metadata store
        if md is not None:
            coll = ic.get_collection(iPath)
            metadata = {
                "iRODS path": coll.path,
                "iRODS server": ic.host,
                "iRODS user": ic.username,
            }
            if config["ELN"]["title"] == '':
                annotateElab(metadata, ic, md, coll, title='Data in iRODS')
            else:
                annotateElab(metadata, ic, md, coll, title=config["ELN"]["title"])

            if os.path.isfile(dataPath):
                item = ic.get_dataobject(
                    coll.path+'/'+os.path.basename(dataPath))
                ic.add_metadata([item], 'ELN', md.metadataUrl)
            elif os.path.isdir(dataPath):
                upColl = ic.get_collection(
                            coll.path+'/'+os.path.basename(dataPath))
                items = [upColl]
                for c, _, objs in upColl.walk():
                    items.append(c)
                    items.extend(objs)
                ic.add_metadata(items, 'ELN', md.metadataUrl)

        print()
        print(BLUE+'Upload complete with the following parameters:')
        print(json.dumps(config, indent=4))
        print(DEFAULT)
        ic.cleanup()
    elif operation == 'download':
        if prepareDownload(irodsPath, ic, config):
            downloadDir = config['DOWNLOAD']['path']
            irodsDataPath = config["iRODS"]["downloadItem"]
            print(YEL,
                  'Downloading: ' + irodsDataPath + ', ' + \
                  str(ic.get_irods_size([irodsDataPath]) * kw.MULTIPLIER) + 'GB',
                  DEFAULT)
            try:
                item = ic.get_collection(irodsDataPath)
            except Exception:
                item = ic.get_dataobject(irodsDataPath)
            print(item, downloadDir)
            ic.download_data(item, downloadDir, ic.get_irods_size([irodsDataPath]), force=False)
            print()
            print(BLUE+'Download complete with the following parameters:')
            print(json.dumps(config, indent=4))
            print(DEFAULT)
            ic.cleanup()
        else:
            ic.cleanup()
            sys.exit(2)
    else:
        print('Not an implemented operation.')
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
