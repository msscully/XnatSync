import sys
import getpass
import re
import dicom
import os
import shutil
import subprocess
import shlex
from pyxnat import Interface
import argparse,tempfile,random
sys.path.append('/paulsen/PREDICT_ORIG_DATA/bin')
import phdUtils
import logging,glob,datetime

class ProjectNameError(Exception):
    def __init__(self, value):
        self.value = value
    def __str__(self):
        return repr(self.value)

class SyncRPACStoPredict:
    def __init__(self,rpacsXnat,rpacsCache,predictXnat,
                 predictDicomRemap,predictBaseAnon,
                 predictDicomScp,insertedAfter):
        self.logger = logging.getLogger('SyncTasks.SyncRpacsToPredict')
        self.logger.debug('Creating an instance of syncRPACStoPredict')
        self.rpacsXnat = rpacsXnat
        self.predictXnat = predictXnat
        self.rpacsProjectName = ''
        self.predictProjectName = ''
        self.predictXnatBaseConditions = []
        self.predictDicomRemap = predictDicomRemap
        self.predictBaseAnon = predictBaseAnon
        self.predictDicomScp = predictDicomScp
        self.rpacsCache = rpacsCache
        self.insertedAfter = insertedAfter

    def syncOneRpacsProjectToPredict(self,rpacsProjectName,predictProjectName):
        self.logger.info('Starting to sync '+rpacsProjectName+" to "+predictProjectName)
        self.rpacsProjectName=rpacsProjectName
        self.predictProjectName=predictProjectName

        if not (re.match('PHD',predictProjectName) or re.match('FMRI',predictProjectName)):
            self.logger.critical("Predict project name passed is invalid")
            raise ProjectNameError("Predict Project name not of the form 'PHD_*' or 'FMRI_*'")
        self.rpacsProjectName = rpacsProjectName
        self.predictProjectName = predictProjectName
        self.predictXnatBaseConditions = [('xnat:mrSessionData/PROJECT','=',predictProjectName),'and']
        rpacsSessions = self.__getRpacsSessions()
        for rpacsSessionData in rpacsSessions:
            self.__checkAndFreeDiskSpace()
            rpacsDate = rpacsSessionData['date']
            rpacsSubjID = rpacsSessionData['subject_id']
            rpacsSubjectLabel = self.rpacsXnat.select.project(rpacsProjectName).subject(rpacsSubjID).attrs.get('xnat:subjectData/label')
            predictSubject = self.predictXnat.select.project(predictProjectName).subject(rpacsSubjectLabel)
            if not predictSubject.exists():
                if re.search('^\d\d\d\d$',rpacsSubjectLabel):
                    # Need to create subject for valid subject labels (\d4)
                    self.logger.info(rpacsSubjectLabel + " does not exist in predict. Creating...")
                    predictSubject.create()
                else:
                    self.logger.info(rpacsSubjectLabel+","+rpacsDate+" goes in HD_PILOT.")
                    continue
            rpacsSession = self.rpacsXnat.select.project(rpacsProjectName).subject(rpacsSubjID).experiment(rpacsSessionData['session_id'])
            studyInstanceUID,studyDate,studyTime = self.__getRpacsStudyParams(rpacsSession)
            predictSessions = self.__getPredictSessions(predictProjectName,rpacsSubjectLabel)
            predictSessionsFMRICompat = self.__getPredictSessions('fMRI_COMPAT',rpacsSubjectLabel)
            predictSessionsHDPilot = self.__getPredictSessions('HDPILOT',rpacsSubjectLabel)
            predictSessionsPHD_000 = self.__getPredictSessions('PHD_000',rpacsSubjectLabel)
            if (self.__isSessionInPredict(predictSessions,studyDate,studyTime) or
                self.__isSessionInPredict(predictSessionsFMRICompat,studyDate,studyTime) or
                self.__isSessionInPredict(predictSessionsPHD_000,studyDate,studyTime) or
                self.__isSessionInPredict(predictSessionsHDPilot,studyDate,studyTime)
               ):
                self.logger.info("{0},{1},{2} Exits in predict, skipping.".format(
                    rpacsSubjectLabel,rpacsDate,studyTime))
                continue
            self.logger.info(rpacsSubjectLabel+","+rpacsDate+","+studyTime+" doesn't exist in predict xnat.")
            # Get the site from the project name
            predictSite = self.__getPredictSite(predictProjectName)

            # Need to generate a scanID
            self.logger.info('Generating a new scan ID.')
            newScanID = phdUtils.getOrCreateScanID(predictSite,rpacsSubjectLabel,rpacsDate,studyTime,studyInstanceUID,'msscully','false')
            self.logger.info("    New scanID="+str(newScanID))

            dicomDirs = self.__downloadScans(rpacsSession)
            self.__uploadScanToPredict(dicomDirs,newScanID,predictProjectName,rpacsSubjectLabel)
            # Remove the temporary directories
            for dir in dicomDirs:
                shutil.rmtree(dir)

    def __checkAndFreeDiskSpace(self):
        diskObj = os.statvfs(self.rpacsCache)
        capacity = diskObj.f_bsize * diskObj.f_blocks
        available = diskObj.f_bsize * diskObj.f_bavail
        percentFree = available / capacity
        # If not enough disk space, delete image files in cache older than 10 minutes.
        if percentFree <= 0.15:
            tempDicoms = glob.glob(self.rpacsCache+"/*.dcm")
            timeThresh = datetime.datetime.now() - datetime.timedelta(minutes=30)
            timeThresh = timeThresh.timetuple()
            for image in tempDicoms:
                imageWithPath = os.path.join(self.rpacsCache,image)
                fileTimeSecs = os.path.getmtime(imageWithPath)
                fileTime = localtime(fileTimeSecs)
                if fileTime <= timeThresh:
                    # File is old, delete it
                    os.remove(imageWithPath)

    def __getPredictSite(self,predictProjectName):
        predictSite = ''
        if re.search('PHD',predictProjectName):
            predictSite = predictProjectName.lstrip('PHD_')
        elif re.search('FMRI',predictProjectName):
            predictSite = predictProjectName.lstrip('FMRI_HD')
        else:
            self.logger.critical("Predict project name passed is invalid")
            raise ProjectNameError("Predict Project name not of the form 'PHD_*' or 'FMRI_*'")
        return predictSite

    def __uploadScanToPredict(self,dicomDirs,newScanID,predictProjectName,rpacsSubjectLabel):
        " upload images to predict xnat"
        self.logger.info("Starting upload of {0} to predict project {1}".format(
            newScanID, predictProjectName))
        dicomRemap = self.predictDicomRemap
        baseAnon = self.predictBaseAnon
        dicomScp = self.predictDicomScp
        tempAnonDir = tempfile.mkdtemp()
        anonScript = tempAnonDir+"/anon-"+predictProjectName+"_"+rpacsSubjectLabel+"_"+str(newScanID)+".das"
        shutil.copy(baseAnon,anonScript)
        anonOut = open(anonScript,'a')
        anonOut.write("// TODO: fix this later\n")
        anonOut.write("//(0008,103e) := $ { SERIES_DESCRIPTION }\n")
        anonOut.write('\n')
        anonOut.write("// Note: this part isn't suitable for DicomRemap, which doesn't\n")
        anonOut.write("// yet handle user-assigned variables.\n")
        anonOut.write("(0020,0010) := \"{0}\"\n".format('site-024'))
        anonOut.write("(0008,0050) := \"{0}\"\n".format(predictProjectName))
        anonOut.write("(0008,1030) := \"{0}\"\n".format(predictProjectName))
        anonOut.write("(0010,0010) := \"{0}\"\n".format(rpacsSubjectLabel))
        anonOut.write("(0010,0020) := \"{0}\"\n".format(newScanID))
        ##  http://www.xnat.org/DicomServer has notes regarding the exact formatting of this field
        anonOut.write("(0010,4000) := \"Project: {0}; Subject: {1}; Session: {2}; AA:true\"\n".format(predictProjectName,rpacsSubjectLabel,newScanID))
        anonOut.close()

        ##  # All the directories to anonimize
        dicomDirs = " ".join(dicomDirs)

        remapCommand=dicomRemap+" -d "+anonScript+" -o "+dicomScp+" "+ dicomDirs
        self.logger.debug(remapCommand)
        command_list=[dicomRemap, '-d '+anonScript, '-o '+dicomScp, dicomDirs]

        try:
            tmpOutput = phdUtils.check_output(command_list,stderr=subprocess.STDOUT)
            if re.search('Exception',tmpOutput):
                self.logger.critical("An exception occurred while running the dicom remap command. Output follows.\n"+tmpOutput)
            self.logger.debug(tmpOutput)
        except Exception as e:
            self.logger.critical("An Exception occurred when running the dicom remap command! " + e)
            exit(1)

    def __downloadScans(self,rpacsSession):
        " download session from rpacs xnat"
        self.logger.info('Downloading scans from rpacs, session {0}.'.format(rpacsSession))
        tempScanDir = tempfile.mkdtemp()
        dicomDirs = []
        rpacsScans = rpacsSession.scans()
        for scan in rpacsScans:
            tempDir = tempfile.mkdtemp(dir=tempScanDir)
            dicomDirs.append(tempDir)
            resource = scan.resource('DICOM')
            files = resource.files()
            for file in files:
              path = os.path.join(tempDir,file.id())
              file.get_copy(path)
              #os.symlink(os.path.join(self.rpacsCache,file.id()),path)
              # This should really be implemented as a getter instead of accessing the private variable!
              #os.symlink(os.path.join(self.rpacsXnat.cache._cache.cache,file.id()),path)
        return dicomDirs

    def __isSessionInPredict(self,predictSessions,studyDate,studyTime):
        self.logger.debug('Does the session exist in predict?')
        for session in predictSessions:
            tempDate = session['date'].replace('-','')
            tempTime = session['time'].replace(':','')
            self.logger.debug('tempDate='+tempDate+', tempTime='+tempTime+', studyDate='+studyDate+', studyTime='+studyTime)
            if tempDate == studyDate and tempTime == studyTime:
                return True
        return False

    def __getRpacsStudyParams(self,rpacsSession):
        rpacsFile = self.__getRandomRpacsDicomFile(rpacsSession)
        tempDicomData = dicom.read_file(rpacsFile.get())
        studyInstanceUID = tempDicomData.StudyInstanceUID
        studyDate = tempDicomData.StudyDate
        studyTime = tempDicomData.StudyTime
        rpacsTime = re.sub('\.\d+','',studyTime)
        return studyInstanceUID,studyDate,rpacsTime

    def __getRandomRpacsDicomFile(self,rpacsSession):
        rpacsScans = rpacsSession.scans().get()
        randomScan = str(random.randint(0,len(rpacsScans)-1))
        rpacsScan = rpacsSession.scan(randomScan)
        rpacsFiles = rpacsSession.scans().resource('DICOM').files()
        rpacsFile = rpacsFiles.first()
        return rpacsFile

    def __getPredictSessions(self,predictProjectName,rpacsSubjectLabel):
        self.logger.info("Fetching predict sessions in {0} for subject {1}".format(
            predictProjectName,rpacsSubjectLabel))
        predictXnatConditions = [('xnat:mrSessionData/PROJECT','=',predictProjectName),'and']
        predictXnatConditions.extend([('xnat:subjectData/label','=',rpacsSubjectLabel),'and'])
        predictSessions=self.predictXnat.select('xnat:mrSessionData').where(predictXnatConditions)
        self.logger.debug('len(predictSessions)='+str(len(predictSessions)))
        #predictSessions=self.predictXnat.select.project(predictProjectName).subject(rpacsSubjectLabel).experiments()
        return predictSessions

    def __getRpacsSessions(self):
        " get all sessions in RPACS for rpacsProjectName"
        self.logger.info('Fetching rpacs sessions')
        xnatBaseConditions = [('xnat:mrSessionData/PROJECT','=',self.rpacsProjectName),'and',
                             ('xnat:mrSessionData/INSERT_DATE','>=',self.insertedAfter),'and']
        xnatConditions = list(xnatBaseConditions)
        xnatSessions=self.rpacsXnat.select.project(self.rpacsProjectName).subjects().experiments().get()
        sessions=self.rpacsXnat.select('xnat:mrSessionData',
                                       ['xnat:mrSessionData/subject_id',
                                        'xnat:mrSessionData/Date',
                                        'xnat:mrSessionData/SESSION_ID']).where(xnatConditions)
        return sessions




def main():
    parser = argparse.ArgumentParser(description='Finds scans in RPACS xnat that are missing from predict xnat and copies them over.')
    group = parser.add_argument_group('Required')
    parser.add_argument('--logFile',action="store",dest='tmpLogFileName',required=False,
                        help='The output log file.',
                        default='/IPLlinux/raid0/homes/hd-mscully/RPACSToPredict.log')
    parser.add_argument('--predictProject',action="store",dest='predictProjectName',required=False,
                        help='The predict project to sync to.',
                       default='FMRI_HD_024')
    parser.add_argument('--rpacsProject',action="store",dest='rpacsProjectName',required=False,
                        help='The rpacs project to sync from.',
                       default='JP_FMRI_HD')
    parser.add_argument('--rpacsXnatUrl',action="store",dest='rpacsXnatUrl',required=False,
                        help='The url for the rpacs xnat instance.',
                        default='https://rpacs.icts.uiowa.edu/xnat')
    parser.add_argument('--rpacsCache',action="store",dest='rpacsCache',required=False,
                        help='The cache for the rpacs pyxnat connection.',
                        default='/tmp/rpacsXnatCache')
    parser.add_argument('--predictXnatUrl',action="store",dest='predictXnatUrl',required=False,
                        help='The url for the predict xnat instance.',
                        default='https://www.predict-hd.net/xnat')
    parser.add_argument('--predictCache',action="store",dest='predictCache',required=False,
                        help='The cache for the predict pyxnat connection.',
                        default='/tmp/predictXnatCache')
    parser.add_argument('--predictDicomRemap',action="store",dest='predictDicomRemap',required=False,
                        help='Location of the DicomRemap progam.',
                        default='/paulsen/PREDICT_ORIG_DATA/DicomBrowser-1.5-SNAPSHOT/bin/DicomRemap')
    parser.add_argument('--predictBaseAnon',action="store",dest='predictBaseAnon',required=False,
                        help='Location of the base anonymization script.',
                        default='/paulsen/etc/new_anon.das')
    parser.add_argument('--predictDicomScp',action="store",dest='predictDicomScp',required=False,
                        help='Url for dicomScp',
                        default='dicom://xnat.predict-hd.net:8104/XNAT')

    inputArguments = parser.parse_args()

    logger = logging.getLogger('SyncTasks')
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(inputArguments.tmpLogFileName)
    fh.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    rpacsXnatUrl = inputArguments.rpacsXnatUrl
    username = raw_input("Enter xnat username: ")
    password = getpass.getpass("Enter password for {0}@{1}: ".format(username,rpacsXnatUrl))

    # setup ssh tunnel.  Requires ssh keys for this user
    sshTunnel = subprocess.Popen(["ssh","-N", "-L",
                                  "25901:localhost:5432",username+"@xnat.predict-hd.net"])

    logger.info('Connecting to rpacs xnat')
    xnatRpacs = Interface(server=rpacsXnatUrl, user=username, password=password,
                     cachedir=inputArguments.rpacsCache)

    logger.info('Connecting to predict xnat')
    xnatUrl = inputArguments.predictXnatUrl
    xnatPredict = Interface(server=xnatUrl, user=username, password=password,
                     cachedir=inputArguments.predictCache)

    logger.debug('Starting to sync rpacs to predict')
    sync = SyncRPACStoPredict(xnatRpacs,inputArguments.rpacsCache,xnatPredict,
                              inputArguments.predictDicomRemap,
                              inputArguments.predictBaseAnon,inputArguments.predictDicomScp)

    sync.syncOneRpacsProjectToPredict(inputArguments.rpacsProjectName,
                                     inputArguments.predictProjectName)

    sshTunnel.terminate()
    xnatPredict.cache.clear()
    xnatRpacs.cache.clear()

if __name__ == "__main__":
    main()
