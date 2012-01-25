import sys
import logging
import logging.handlers
import ConfigParser
import argparse
import getpass
import keyring
import datetime
import subprocess
import shlex
import smtplib
from email.mime.text import MIMEText
from pyxnat import Interface

import SyncNewPredictDataToMRx
import SyncRPACSToPredict

def setup_email_logging_handler(smtp_host, sync_from_address, to_email_list, email_subject, formatter,
                                email_log_level):

    email_handler = logging.handlers.SMTPHandler(smtp_host,
                                                 sync_from_address,
                                                 to_email_list,
                                                 email_subject)

    email_handler.setLevel(email_log_level)
    email_handler.setFormatter(formatter)

    return email_handler


if __name__ == "__main__":
    # Create and parse input arguments
    parser = argparse.ArgumentParser(description='')
    group = parser.add_argument_group('Required')
    group.add_argument('--configFile', action="store", dest='config_file',
                       required=False,
                       help='The file containing the configurations for the' +
                       ' syncronization tasks.',
                      default='syncConfig.cfg')
    parser.add_argument('--setPassword', action="store_true", dest='set_password', required=False,
                        default=False,
                        help='Include this flag to be prompted to update the'+
                        'password.')
    input_arguments = parser.parse_args()

    # Need to parse config file
    config = ConfigParser.ConfigParser()
    config.read(input_arguments.config_file)
    
    # Email config parameters 
    log_file_w_path=config.get('Logging','LogFilePath')
    log_level=config.get('Logging','LogLevel')
    warn_email_list=shlex.split(config.get('Logging','WarnEmailList'),',')
    error_email_list=shlex.split(config.get('Logging','ErrorEmailList'),',')
    critical_email_list=shlex.split(config.get('Logging','CriticalEmailList'),',')
    summary_email_list=shlex.split(config.get('Logging','SummaryEmailList'),',')
    summary_email_subject = config.get('Logging','SummaryEmailSubject')
    warn_subject = config.get('Logging','WarnSubject')
    error_subject = config.get('Logging','ErrorSubject')
    critical_subject = config.get('Logging','CriticalSubject')
    smtp_host = config.get('Logging','SMTPHost')
    sync_from_address = config.get('Logging','FromAddress')

    LOGGING_LEVELS = {'critical': logging.CRITICAL,
                      'error': logging.ERROR,
                      'warning': logging.WARNING,
                      'info': logging.INFO,
                      'debug': logging.DEBUG}

    logger = logging.getLogger('SyncTasks')
    logger.setLevel(LOGGING_LEVELS[log_level.lower()])
    fh = logging.FileHandler(log_file_w_path)
    fh.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    
    summary_text = 'Synchronization successfull!'

    sshTunnel = None
    xnat_predict = None
    xnat_rpacs = None
    try:
        if warn_email_list:
            warn_email_handler = setup_email_logging_handler(smtp_host,
                                                             sync_from_address,
                                                             warn_email_list,
                                                             warn_subject,
                                                             formatter,
                                                             logging.WARN)
            logger.addHandler(warn_email_handler)
        
        if error_email_list:
            error_email_handler = setup_email_logging_handler(smtp_host,
                                                              sync_from_address,
                                                              error_email_list,
                                                              error_subject,
                                                              formatter,
                                                              logging.ERROR)
            logger.addHandler(error_email_handler)

        if critical_email_list:
            critical_email_handler = setup_email_logging_handler(smtp_host,
                                                                 sync_from_address,
                                                                 critical_email_list,
                                                                 critical_subject,
                                                                 formatter,
                                                                 logging.CRITICAL)
            logger.addHandler(critical_email_handler)


        # Programs config parameters
        dicom_to_nrrd_converter_w_path = config.get('Programs',
                                              'DicomToNrrdConverterPath')
        mri_convert_w_path = config.get('Programs', 'MriConvertPath')
        convert_between_file_formats_w_path = config.get('Programs',
                                                     'ConvertBetweenFileFormatsPath')

        # Predict config parameters
        xnat_predict_url = config.get('XnatPredict','XNATURL')
        predict_username = config.get('XnatPredict','USERNAME')
        predict_cache = config.get('XnatPredict','CACHEDIR')
        predict_dicom_scp = config.get('XnatPredict','DICOM_SCP')

        # Rpcas config paramaters
        xnat_rpacs_url = config.get('XnatRpacs','XNATURL')
        rpacs_username = config.get('XnatRpacs','USERNAME')
        rpacs_cache = config.get('XnatRpacs','CACHEDIR')

        # RpacsToPredict config parameters
        rpacs_projects = config.get('RpacsToPredict','RpacsProjects')
        predict_projects = config.get('RpacsToPredict','PredictProjects')

        # DicomRemap config parameters
        dicom_remap_command = config.get('DicomRemap','REMAPCOMMAND')
        base_anon = config.get('DicomRemap','BASEANON')

        # Misc config parameters
        destination_base = config.get('Misc','DestinationBase')
        white_list_file_w_path = config.get('Misc','WhiteListPath')
        new_scan_interval = config.get('Misc', 'NewScanInterval')
        ssh_username = config.get('Misc', 'SSHUsername')

        # Update the password in the keyring
        if (input_arguments.set_password):
            password = getpass.getpass('Predict Xnat password: ')
            keyring.set_password('RunSyncronization',predict_username,password)
            password = getpass.getpass('Rpacs Xnat password: ')
            keyring.set_password('RunSyncronization',rpacs_username,password)
            sys.exit()

        # Need to grab the password from the keyring, as this is running unattended
        # as a cron job
        predict_password = keyring.get_password('RunSyncronization',predict_username)
        rpacs_password = keyring.get_password('RunSyncronization',rpacs_username)

        logger.info('Creating predict xnat interface.')
        xnat_predict = Interface(server=xnat_predict_url, user=predict_username,
                                 password=predict_password,
                                 cachedir=predict_cache)

        xnat_rpacs = Interface(server=xnat_rpacs_url, user=rpacs_username,
                               password=rpacs_password,
                               cachedir=rpacs_cache)

        # setup ssh tunnel.  Requires ssh keys for this user
        sshTunnel = subprocess.Popen(["ssh","-N", "-L",
                                      "25901:localhost:5432",ssh_username+"@xnat.predict-hd.net"])

        todays_date = datetime.datetime.now()
        target_date = todays_date - datetime.timedelta(int(new_scan_interval))
        inserted_after = target_date.strftime('%Y%m%d')

        sync = SyncRPACSToPredict.SyncRPACStoPredict(xnat_rpacs,rpacs_cache,xnat_predict,
                                                     dicom_remap_command,
                                                     base_anon,
                                                     predict_dicom_scp,
                                                     inserted_after)

        for rpacs_project_name,predict_project_name in zip(rpacs_projects.split(','), 
                                                           predict_projects.split(',')):
            sync.syncOneRpacsProjectToPredict(rpacs_project_name,
                                              predict_project_name)
            #pass


        syncData = SyncNewPredictDataToMRx.SyncNewPredictDataToMRx(xnat_predict,predict_cache,
                                                                   white_list_file_w_path,
                                                                   destination_base,
                                                                   inserted_after,
                                                                   mri_convert_w_path,
                                                                   convert_between_file_formats_w_path,
                                                                   dicom_to_nrrd_converter_w_path)

        syncData.syncAllSessions()

    except Exception, excep:
        logger.critical("something raised an exception: " + str(excep),exc_info=True)
        summary_text = 'Synchronization FAILED!'
        summary_email_subject = "FAILED - %s" % (summary_email_subject)

    # Generate summary email message
    if summary_email_list:
        summary_message = MIMEText(summary_text, 'plain')  
        summary_message['To'] = ",".join(summary_email_list)
        summary_message['From'] = sync_from_address
        summary_message['Subject'] = summary_email_subject

        email_server=smtplib.SMTP("ns-mx.uiowa.edu")
        email_server.sendmail(sync_from_address,summary_email_list,summary_message.as_string())

    if sshTunnel:
        sshTunnel.terminate()
    if xnat_predict:
        xnat_predict.cache.clear()
    if xnat_rpacs:
        xnat_rpacs.cache.clear()
