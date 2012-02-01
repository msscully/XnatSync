import os
import sys
import argparse
import getpass
import subprocess
import tempfile
import shutil
import re
import dicom
import glob,datetime,stat,logging
import httplib2
from time import localtime
from pyxnat import Interface
try:
  sys.path.append('/paulsen/PREDICT_ORIG_DATA/bin')
  import phdUtils
except:
  print "Error: Module 'phdUtils' not found."
  sys.exit(1)

class SyncNewPredictDataToMRx():
    def __init__(self,xnat,xnatCacheDir,whiteListFileName,
                 destinationBase,insertedAfter,mriConvertPath,
                 convertBetweenFormatsPath,dicomToNrrdPath):
        self.logger = logging.getLogger('SyncTasks.SyncNewPredictDataToMRx')
        self.logger.info('Creating an instance of syncRPACStoPredict')
        self.xnat = xnat
        self.xnatCacheDir = xnatCacheDir
        self.destinationBase=destinationBase
        self.insertedAfter = insertedAfter
        self.mriConvertPath = mriConvertPath
        self.convertBetweenFormatsPath = convertBetweenFormatsPath
        self.dicomToNrrdPath = dicomToNrrdPath
        # Read in the scan type white list
        whiteListFile = open(whiteListFileName, 'r')
        self.whiteList = {}
        for line in whiteListFile:
            self.whiteList[line.rstrip('\r\n')] = 1;

    def __getScanTypeFromXnat(self,projectLabel,scanID,seriesNumber,subjID):
        scanType = ''
        scanObj=self.xnat.select.project(projectLabel).subject(subjID).experiment(scanID).scan(seriesNumber)
        scanType = scanObj.attrs.get('type')
        return scanType

    def __convertDicomToNrrd(self,dicomDir,convertedFileNameWithPath,seriesNumber):
        nrrdList = glob.glob(os.path.dirname(convertedFileNameWithPath)+'/*_'+seriesNumber+'.nrrd')
        if os.path.exists(convertedFileNameWithPath) or len(nrrdList) > 0:
            self.logger.info(convertedFileNameWithPath+" already exists, no conversion needed.")
            numVols = re.search("DWI-(\d+)",convertedFileNameWithPath).group(1)
            return numVols
        #commandList = ["/scratch/msscully/development/DicomToNrrd/StandAloneDicomToNrrdConverter-build/bin/DicomToNrrdConverter"]
        commandList = [self.dicomToNrrdPath]
        commandList.append("--inputDicomDirectory")
        commandList.append(dicomDir)
        commandList.append("--outputVolume")
        commandList.append(convertedFileNameWithPath)
        # Will only affect Seimens data
        commandList.append("--useBMatrixGradientDirections")
        self.logger.info(" ".join(commandList))
        self.logger.info("Converting "+dicomDir+" to "+convertedFileNameWithPath)
        my_env = os.environ
        my_env['FREESURFER_HOME']='/opt/freesurfer'
        #output = phdUtils.check_output(commandList,stderr=sys.stdout,env=my_env)
        output = phdUtils.check_output(commandList,env=my_env)
        self.logger.info(output)

        searchMatch = re.search("Number of usable volumes\: (\d+)",output)
        if searchMatch:
            numberVolumes = searchMatch.group(1)
            return numberVolumes
        else:
            #TODO Decide if this is really an error and handle it more gracefully
            self.logger.warn("Couldn't find number of volumes. Error?")
            exit(1)

    def __updateDWIScanTypeInXnat(self,projectLabel,scanID,seriesNumber,newScanType):
        self.xnat.select.project(projectLabel).subjects().experiment(scanID).scans(seriesNumber).get(1)[0].attrs.set('type',newScanType)
        self.xnat.select.project(projectLabel).subjects().experiment(scanID).scans(seriesNumber).get(1)[0].attrs.set('corrected_type',newScanType)

    def __convertDicomToMGZ(self,dicomDir,convertedFileNameWithPath):
        if os.path.exists(convertedFileNameWithPath):
            self.logger.info(convertedFileNameWithPath+" already exists, no conversion needed.")
            return
        #commandList = ["/opt/freesurfer/bin/mri_convert"]
        commandList = [self.mriConvertPath]
        commandList.append("-it")
        commandList.append("dicom")
        commandList.append(dicomDir)
        commandList.append(convertedFileNameWithPath)
        self.logger.info("Converting "+dicomDir+" to "+convertedFileNameWithPath)
        self.logger.info(" ".join(commandList))
        my_env = os.environ
        my_env['FREESURFER_HOME']='/opt/freesurfer'
        try:
            self.logger.info(phdUtils.check_output(commandList,stderr=sys.stdout,env=my_env))
        except subprocess.CalledProcessError as e:
            self.logger.warn("mri_convert threw an exception." + str(e),exc_info=True)

    def __convertDicomToNifti(self,dicomDir,convertedFileNameWithPath):
        if os.path.exists(convertedFileNameWithPath):
            self.logger.info(convertedFileNameWithPath+" already exists, no conversion needed.")
            return
        #commandList = ["/opt/brains2/bin/ConvertBetweenFileFormats"]
        commandList = [self.convertBetweenFormatsPath]
        commandList.append(dicomDir)
        commandList.append(convertedFileNameWithPath)
        self.logger.info(" ".join(commandList))
        self.logger.info("Converting "+dicomDir+" to "+convertedFileNameWithPath)
        try:
            self.logger.info(phdUtils.check_output(commandList,stderr=sys.stdout))
        except subprocess.CalledProcessError as e:
            self.logger.error("ConvertBetweenFileFormats threw an exception.")
            self.logger.error(e)

    def __downloadScan(self,session,seriesNumber):
        tempDir = tempfile.mkdtemp()
        project = self.xnat.select.project(session['project'])
        subject = project.subject(session['subject_id'])
        session = subject.experiment(session['session_id'])
        resource = session.scan(seriesNumber).resource('DICOM')
        files = resource.files()
        for file in files:
            path = os.path.join(tempDir,file.id())
            file.get_copy(path)

        return tempDir

    def __convertNiftiToMGZ(self,niftiGZFileNameWithPath,newMGZFileNameWithPath,tempDir):
        if os.path.exists(newMGZFileNameWithPath):
            self.logger.info(newMGZFileNameWithPath+" already exists, no conversion needed.")
            return
        # Need to unzip the .nii.gz version
        commandList = ['gunzip','-f',niftiGZFileNameWithPath]
        self.logger.info(phdUtils.check_output(commandList,stderr=sys.stdout))

        niftiFileName = niftiGZFileNameWithPath.rstrip('.gz')

        dicomFiles = os.listdir(tempDir)
        data = dicom.read_file(os.path.join(tempDir,dicomFiles[0]))
        te = data.EchoTime
        tr = data.RepetitionTime
        flip = data.FlipAngle

        # Pass all the above to mri_convert
        commandList = ["/opt/freesurfer/bin/mri_convert"]
        commandList.append('-te')
        commandList.append(str(te))
        commandList.append('-tr')
        commandList.append(str(tr))
        if 'InversionTime' in data:
            commandList.append('-TI')
            commandList.append(str(data.InversionTime))
        commandList.append('-flip_angle')
        commandList.append(str(flip))
        commandList.append("-it")
        commandList.append("nii")
        commandList.append(niftiFileName)
        commandList.append(newMGZFileNameWithPath)
        self.logger.info("Converting "+niftiGZFileNameWithPath+" to "+newMGZFileNameWithPath)
        #print " ".join(commandList)
        self.logger.info(phdUtils.check_output(commandList,stderr=sys.stdout))

        # Re-zip the .nii file
        commandList = ['gzip',niftiFileName]
        self.logger.info(phdUtils.check_output(commandList,stderr=sys.stdout))

    def __getRecentSessions(self,projectLike):
        self.logger.info('Fetching new sessions in projects like '+projectLike)
        newScanConditions=[('xnat:mrSessionData/PROJECT','LIKE',projectLike),'and',('xnat:mrSessionData/INSERT_DATE','>=',self.insertedAfter)]
        self.logger.debug('scanConditions=[(\'xnat:mrSessionData/PROJECT\',\'LIKE\','+projectLike+'),\'and\',(\'xnat:mrSessionData/INSERT_DATE\',\'>=\','+self.insertedAfter+')]')
        phdSessions = self.xnat.select('xnat:mrSessionData').where(newScanConditions)
        return phdSessions

    def __getRecentPredictSessions(self):
        return self.__getRecentSessions('%PHD%')

    def __getRecentFMRISessions(self):
        return self.__getRecentSessions('%FMRI_%')

    def __checkAndFreeDiskSpace(self):
        diskObj = os.statvfs(self.xnatCacheDir)
        capacity = diskObj.f_bsize * diskObj.f_blocks
        available = diskObj.f_bsize * diskObj.f_bavail
        percentFree = available / capacity
        # If not enough disk space, delete image files in cache older than 10 minutes.
        if percentFree <= 0.15:
            tempNiftis = glob.glob(self.xnatCacheDir+"/*.nii.gz")
            timeThresh = datetime.datetime.now() - datetime.timedelta(minutes=30)
            timeThresh = timeThresh.timetuple()
            for image in tempNiftis:
                imageWithPath = os.path.join(self.xnatCacheDir,image)
                fileTimeSecs = os.path.getmtime(imageWithPath)
                fileTime = localtime(fileTimeSecs)
                if fileTime <= timeThresh:
                    # File is old, delete it
                    os.remove(imageWithPath)
    
    def __isScanUsableInXnat(self,projectLabel,scanID,
                             seriesNumber,subjectLabel):
        usable = False;
        tmpExperiment = self.xnat.select.project(projectLabel).subject(subjectLabel).experiment(scanID)
        usableText = tmpExperiment.scan(seriesNumber).attrs.get('xnat:mrScanData/quality')
        if (usableText == 'usable') or (usableText == 'VIExcellent'):
            usable = True
        elif usableText == 'VIUnusable':
            usable = False
        elif usableText == 'VIQuestionable':
            usable = False
        elif int(usableText) >= 5:
            usable = True;

        return usable


    def syncAllSessions(self):
        """ This syncs all sessions archived after insertedDate.  PHD and FMRI are
            done seperately to make it easier to fetch recent scans in projects
            we care about."""
        self.logger.info('Fetching all recent PHD_* sessions.')
        phdSessions = self.__getRecentPredictSessions()
        self.logger.info('Syncing all recent PHD_* sessions.')
        self.__syncSessions(phdSessions)
        self.logger.info('Fetching all recent FMRI_* sessions.')
        fmriSessions = self.__getRecentFMRISessions()
        self.logger.info('Syncing all recent FMRI_* sessions.')
        self.__syncSessions(fmriSessions)

    def __syncSessions(self,sessions):
        """Convert relevant scans in passed sessions and write to destinationBase.
        """
        for session in sessions:
            # Check if there is enough disk space available
            self.__checkAndFreeDiskSpace()

            # Get scanID, sebjectLabel, and projectLabel
            projectLabel = session['project']
            tempProject = self.xnat.select.project(projectLabel)
            tempSubject = tempProject.subject(session['subject_id'])
            subjectLabel = tempSubject.attrs.get('xnat:subjectData/label')
            tempExperiment = tempSubject.experiment(session['session_id'])
            scanID = tempExperiment.attrs.get('xnat:mrSessionData/label')

            self.logger.debug('Syncing session: '+ projectLabel+', '+subjectLabel+', '+scanID)
            scanDir = os.path.join(self.destinationBase,projectLabel+
                                   '/'+subjectLabel+'/'+scanID)

            newDir = os.path.join(scanDir,'ANONRAW')

            if os.path.exists(newDir):
                # When commented out it's so that even if some images are there everything
                # will be checked.  In the convert functions it checks for the
                # existence of images before converting, so this shouldn't be too
                # big of a problem.
                if os.listdir(newDir):
                        self.logger.info(projectLabel+","+subjectLabel+","+scanID+" already converted")
                        #continue
            else:
                self.logger.info("Creating new directory: %s" % (newDir))
                os.makedirs(newDir)

            seriesNumbers = tempExperiment.scans().get()
            field_strength = 0

            for seriesNumber in seriesNumbers:
                scanType = self.__getScanTypeFromXnat(projectLabel,
                                               scanID,seriesNumber,subjectLabel)

                # We don't need to download localizers or non image data.
                if re.search('localizer',scanType) or re.search('nonImageDicom',scanType):
                    self.logger.debug("Skipping {0},{1} because it is a localizer/nonImageDicom".format(scanID,seriesNumber))
                    continue

                # Check scanType against whitelist
                if not scanType in self.whiteList:
                    self.logger.info("scanType, " + scanType + ", not in the whitelist, skipping scanID="+scanID+", seriesNumber="+seriesNumber+".")
                    continue

                scan = tempExperiment.scans(seriesNumber).get()
                try:
                    field_strength = tempExperiment.scan(scan).attrs.get('xnat:mrScanData/fieldStrength')
                except IndexError:
                    pass

                # Check if this scan is usable.
                usable = self.__isScanUsableInXnat(projectLabel,scanID,
                                                   seriesNumber,subjectLabel)

                # Check if this series number has already been converted
                convertedFilesList = []
                if re.search('DWI',scanType):
                  convertedFilesList = glob.glob(newDir+'/*_'+seriesNumber+'.nrrd')
                else:
                  self.logger.debug(newDir+'/*_'+seriesNumber+'.nii.gz')
                  convertedFilesList = glob.glob(newDir+'/*_'+seriesNumber+'.nii.gz')

                self.logger.debug("List of coverted files with this series number: ".format(convertedFilesList))
                if len(convertedFilesList) > 0:
                  self.logger.info("{0},{1} has already been converted.".format(scanID,seriesNumber))
                  if not usable:
                      self.logger.info("{0},{1} is not usable, deleting from filesystem.".format(scanID,seriesNumber))
                      for imageFile in convertedFilesList:
                          self.logger.info("Prepending 'unusable' to {0}".format(imageFile))
                          extension = re.match('[-\w]+\.(.+)$',os.path.basename(imageFile)).group(1)
                          unusableImageFile = os.path.join(newDir,
                                                           'unusable_{0}_{1}_{2}_{3}.{4}'.format(subjectLabel,
                                                                                                 scanID,
                                                                                                 scanType,
                                                                                                 seriesNumber,
                                                                                                 extension))
                          os.rename(imageFile,unusableImageFile)
                  else:
                      for imageFile in convertedFilesList:
                          if re.match('unusable_*',imageFile):
                              self.logger.info("Scan {0} is now usable, removing 'unusable_'.".format(imageFile))
                              extension = re.match('[-\w]+\.(.+)$',os.path.basename(imageFile)).group(1)
                              usableImageFile = os.path.join(newDir,
                                                             '{0]_{1}_{2}_{3}.{4}'.format(subjectLabel,
                                                                                          scanID,
                                                                                          scanType,
                                                                                          seriesNumber,
                                                                                          extension))
                  continue

                unusablePrepend = ''
                if not usable:
                    self.logger.info("{0},{1} has been labeled unusable in xnat.".format(scanID,seriesNumber))
                    unusablePrepend = 'unusable_'

                # download scan from xnat
                self.logger.debug("Downloading {0},{1} from xnat.".format(scanID,
                                                                          seriesNumber))
                try:
                    tempDir = self.__downloadScan(session,seriesNumber)
                except httplib2.HttpLib2Error:
                    self.logger.error("404 error when Downloading {0},{1} from xnat.".format(scanID,seriesNumber))
                    continue

                # Check if scan is PD/T2, T1, or DWI
                if re.search('PD',scanType):
                    self.logger.debug("scanType, {0}, is a 'PD'".format(scanType))
                    #Create two temp directories
                    tmpPDDir = tempfile.mkdtemp()
                    tmpT2Dir = tempfile.mkdtemp()

                    # Put T2 files in one tmp dir and PD files in the other
                    # Shortest TR time is the PD
                    dicomDir = tempDir
                    dicomFiles = os.listdir(dicomDir)
                    scanTypeSuffix = re.search("(\-\d\d)",scanType).group()

                    try:
                      for slice in dicomFiles:
                          if re.search(".xml$",slice):
                              continue
                          try:
                              ds = dicom.read_file(os.path.join(dicomDir,slice))
                          except:
                              self.logger.critical("EXCEPTION CAUGHT WHEN TRYING TO READ FILE {0}".format(
                                  os.path.join(dicomDir,slice)))
                              raise
                          if 'StudyInstanceUID' not in ds:
                              self.logger.warn("{0} does not appear to be a valid dicom file!".format(slice))
                              continue
                          if ds.EchoNumbers == 1:
                              os.symlink(os.path.join(dicomDir,slice),
                                         os.path.join(tmpPDDir,slice))
                          else:
                              os.symlink(os.path.join(dicomDir,slice),
                                         os.path.join(tmpT2Dir,slice))
                    except:
                        self.logger.warn("EXCEPTION CAUGHT READING DICOM FILES in %s" % (dicomDir))
                        self.logger.warn("Skipping {0},{1}".format(scanID,
                                                                   seriesNumber))
                        continue

                    scanTypePD = "PD" + scanTypeSuffix
                    scanTypeT2 = "T2" + scanTypeSuffix
                    newPDFileName=unusablePrepend+subjectLabel+"_"+scanID+"_"+scanTypePD+"_"+seriesNumber+".nii.gz"
                    newT2FileName=unusablePrepend+subjectLabel+"_"+scanID+"_"+scanTypeT2+"_"+seriesNumber+".nii.gz"

                    # Convert PD dicom to nifti
                    try:
                        self.__convertDicomToNifti(tmpPDDir,os.path.join(newDir,newPDFileName))
                    except subprocess.CalledProcessError:
                        self.logger.error("Error converting "+scanID+","+scanTypePD+" to "+os.path.join(newDir,newPDFileName))
                        continue
                    if not os.path.exists(os.path.join(newDir,newPDFileName)):
                        self.logger.error(os.path.join(newDir,newPDFileName)+" doesn't exist! Conversion failed.")
                        continue

                    # Convert T2 dicom to nifti
                    try:
                        self.__convertDicomToNifti(tmpT2Dir,os.path.join(newDir,newT2FileName))
                    except subprocess.CalledProcessError:
                        self.logger.error("Error converting "+scanID+","+scanTypeT2+" to "+os.path.join(newDir,newT2FileName))
                        continue
                    if not os.path.exists(os.path.join(newDir,newT2FileName)):
                        self.logger.error(os.path.join(newDir,newT2FileName)+" doesn't exist! Conversion failed.")
                        continue

                    # Delete the temporary directories
                    shutil.rmtree(tmpPDDir)
                    shutil.rmtree(tmpT2Dir)
                elif re.search('DWI',scanType):
                    newFileName=unusablePrepend+subjectLabel+"_"+scanID+"_"+scanType+"_"+seriesNumber+".nrrd"
                    newFileNameWithPath=os.path.join(newDir,newFileName)

                    # Use DicomToNrrdConverter to convert the DWI to nifti
                    try:
                        numberVolumes = self.__convertDicomToNrrd(tempDir,
                                                newFileNameWithPath,seriesNumber)
                    except subprocess.CalledProcessError:
                        self.logger.error("Error converting "+scanID+","+scanType+","+seriesNumber+" to "+newFileNameWithPath)
                        continue
                    if not os.path.exists(newFileNameWithPath):
                        self.logger.error(newFileNameWithPath+" doesn't exist! Conversion failed.")
                        continue
                    newScanType="DWI-"+numberVolumes
                    correctedFileName=unusablePrepend+subjectLabel+"_"+scanID+"_"+newScanType
                    correctedFileName+="_"+seriesNumber+".nrrd"
                    correctedFileNameWithPath=os.path.join(newDir,correctedFileName)
                    os.rename(newFileNameWithPath,correctedFileNameWithPath)
                    if newScanType != scanType:
                        self.__updateDWIScanTypeInXnat(projectLabel,scanID,seriesNumber,newScanType)
                else:
                    newFileName=unusablePrepend+subjectLabel+"_"+scanID+"_"+scanType+"_"+seriesNumber+".nii.gz"
                    newFileNameWithDir = os.path.join(newDir,newFileName)

                    # Use ConvertBetweenFileFormats to convert the Dicom to nifti
                    try:
                        self.__convertDicomToNifti(tempDir,newFileNameWithDir)
                    except subprocess.CalledProcessError:
                        self.logger.error("Error converting "+scanID+","+scanType+","+seriesNumber+" to "+newFileNameWithDir)
                        continue
                    if not newFileNameWithDir:
                        self.logger.error(newFileNameWithDir+" doesn't exist! Conversion failed.")
                        continue

                if re.match("T1",scanType):
                    newMGZFileName=unusablePrepend+subjectLabel+"_"+scanID+"_"+scanType+"_"+seriesNumber+".mgz"
                    newMGZDir=newDir
                    tmpFiles=[i for i in tempDir if not re.search('xml',i) and
                            not re.search('info',i)]
                    newMGZFileNameWithPath = os.path.join(newMGZDir,newMGZFileName)
                    try:
                        self.__convertDicomToMGZ(tempDir,newMGZFileNameWithPath)
                    except subprocess.CalledProcessError:
                        errorMsg="Problem converting dicom "+scanID+","+scanType+","
                        errorMsg+=seriesNumber+" to "+newMGZFileNameWithPath+". "
                        errorMsg+="Trying to convert the .nii.gz file to .mgz"
                        self.logger.warn(errorMsg)
                    if not os.path.exists(newMGZFileNameWithPath):
                        self.__convertNiftiToMGZ(os.path.join(newMGZDir,newFileName),
                                                 newMGZFileNameWithPath,tempDir)
                    if not newMGZFileNameWithPath:
                        self.logger.error(newMGZFileNameWithPath+" doesn't exist! Conversion failed.")
                        continue

                # Delete the temp download directory
                self.logger.info("Deleting the temporary download directory: {0}".format(tempDir))
                shutil.rmtree(tempDir)

            # Set permissions
            permissions = int("750",8)
            if stat.S_IMODE(os.stat(newDir).st_mode) != permissions:
                self.logger.info("Updating permissions on {0}".format(newDir))
                os.chmod(newDir,permissions)

            # Update session level field strength in xnat if it's missing
            current_field_strength = tempExperiment.attrs.get('xnat:mrSessionData/fieldStrength')
            if current_field_strength == '':
                tempExperiment.attrs.set('xnat:mrSessionData/fieldStrength',field_strength)

        return

if __name__ == "__main__":
    # Create and parse input arguments
    parser = argparse.ArgumentParser(description='')
    group = parser.add_argument_group('Required')
    group.add_argument('--logFile', action="store", dest='logFile', required=True,
                       help='The file to log output to.')
    group.add_argument('--destinationBase', action="store",
                       dest='destinationBase', required=True, help='The base destination directory that will have site/subjid/scanid/ANONRAW built on top of it.')
    group.add_argument('--insertedAfter', action="store",
                       dest='insertedAfter', required=True, help='Only look at scans inserted after this date')
    group.add_argument('--whiteList', action="store", dest='whiteList', required=True,
                       help='The file containing the approved scan types.')
    group.add_argument('--mriConvertPath', action="store", dest='mriConvertPath', required=True,
                       help='The path and command for mri_convert.')
    group.add_argument('--convertBetweenFormatsPath', action="store", dest='convertBetweenFormatsPath', required=True,
                       help='The path and command for ConvertBetweenFileFormats.')
    group.add_argument('--dicomToNrrdPath', action="store", dest='dicomToNrrdPath', required=True,
                       help='The path and command for DicomToNrrdConverter.')
    parser.add_argument('--predictXnatUrl',action="store",dest='predictXnatUrl',required=False,
                        help='The url for the predict xnat instance.',
                        default='https://www.predict-hd.net/xnat')
    parser.add_argument('--predictCache',action="store",dest='predictCache',required=False,
                        help='The cache for the predict pyxnat connection.',
                        default='/tmp/predictXnatCache')
    parser.add_argument('--version', action='version', version='%(prog)s 1.0')
    inputArguments = parser.parse_args()

    logger = logging.getLogger('SyncTasks')
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(inputArguments.logFile)
    fh.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    xnatUrl = inputArguments.predictXnatUrl
    username = raw_input("Enter user: ")

    password = getpass.getpass("Enter password for {0}@{1}: ".format(username,xnatUrl))

    logger.info('Creating predict xnat interface.')
    xnat = Interface(server=xnatUrl, user=username, password=password,
                     cachedir=inputArguments.predictCache)

    mriConvertPath = inputArguments.mriConvertPath
    convertBetweenFormatsPath = inputArguments.convertBetweenFormatsPath
    dicomToNrrdPath = inputArguments.dicomToNrrdPath

    syncData = SyncNewPredictDataToMRx(xnat,inputArguments.predictCache,
                                       inputArguments.whiteList,inputArguments.destinationBase,
                                       inputArguments.insertedAfter,mriConvertPath,
                                       convertBetweenFormatsPath,dicomToNrrdPath)

    syncData.syncAllSessions()

    xnat.cache.clear()
