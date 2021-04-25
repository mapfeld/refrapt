"""Classes for abstraction and use with Refrapt."""

from enum import Enum
import logging
import os
import multiprocessing
import re
from functools import partial
import tqdm
import filelock
from dataclasses import dataclass
import collections
from pathlib import Path

from refrapt.helpers import SanitiseUri
from refrapt.settings import Settings

logger = logging.getLogger(__name__)

class SourceType(Enum):
    """Distinguish between Binary and Source mirrors."""
    Bin = 0
    Src = 1

class UrlType(Enum):
    """Type of downloadable files."""
    Index       = 0
    Translation = 1
    Dep11       = 2
    Archive     = 3

class IndexType(Enum):
    """Type of Index files."""
    Index   = 0
    Release = 1
    Dep11   = 2

class Source:
    """Represents a Source as defined the Configuration file."""
    def __init__(self, line, defaultArch):
        """Initialises a Source a line from the Configuration file and the default Architecture."""
        self._sourceType = SourceType.Bin
        self._architectures = [] # type: list[str]
        self._uri = None
        self._distribution = None
        self._components = [] # type: list[str]
        self._clean = True
        self._flatRepository = False

        # Remove any inline comments
        if "#" in line:
            line = line[0:line.index("#")]

        # Break down the line into its parts
        elements = line.split(" ")
        elements = list(filter(None, elements))

        # Determine Source type
        if elements[0] == "deb":
            self._sourceType = SourceType.Bin
        elif 'deb-src' in elements[0]:
            self._sourceType = SourceType.Src

        elementIndex = 1

        # If Architecture(s) is specified, store it, else set the default
        if "[" in line and "]" in line:
            # Architecture is defined
            archList = line.split("[")[1].split("]")[0].replace("arch=", "")
            self._architectures = archList.split(",")
            elementIndex += 1
        else:
            self._architectures.append(defaultArch)

        self._uri           = elements[elementIndex]

        # Handle flat repositories
        if len(elements) > elementIndex + 1:
            self._distribution = elements[elementIndex + 1]
            self._components   = elements[elementIndex + 2:]
        else:
            self._distribution = ""
            self._components = ["Flat"]
            self._flatRepository = True

        self._indexCollection = IndexCollection(self._components, self._architectures)

        logger.debug("Source")
        logger.debug(f"\tKind:         {self._sourceType}")
        logger.debug(f"\tArch:         {self._architectures}")
        logger.debug(f"\tUri:          {self._uri}")
        logger.debug(f"\tDistribution: {self._distribution}")
        logger.debug(f"\tComponents:   {self._components}")
        logger.debug(f"\tIndex Coll:   {self._indexCollection}")
        logger.debug(f"\tFlat:         {self._flatRepository}")

    def GetIndexes(self) -> list:
        """Get a list of all Indexes for the Source."""
        baseUrl = self._uri + "/dists/" + self._distribution + "/"

        indexes = []
        compressionFormats = [".gz", ".bz2", ".xz"]

        if self._components:
            indexes.append(baseUrl + "InRelease")
            indexes.append(baseUrl + "Release")
            indexes.append(baseUrl + "Release.gpg")
        else:
            # Flat Repositories
            indexes.append(self._uri + "/" + self._distribution + "/InRelease")
            indexes.append(self._uri + "/" + self._distribution + "/Release")
            indexes.append(self._uri + "/" + self._distribution + "/Release.gpg")
            for compressionFormat in compressionFormats:
                indexes.append(self._uri + "/" + self._distribution + "/Sources" + compressionFormat)
                indexes.append(self._uri + "/" + self._distribution + "/Packages" + compressionFormat)
                self._indexCollection.Add(self._components[0], self._architectures[0], self._uri + "/" + self._distribution + "/Sources" + compressionFormat)
                self._indexCollection.Add(self._components[0], self._architectures[0], self._uri + "/" + self._distribution + "/Packages" + compressionFormat)

        if self._sourceType == SourceType.Bin:
            # Binary Files
            if self._components:
                if Settings.Contents():
                    for architecture in self._architectures:
                        for compressionFormat in compressionFormats:
                            indexes.append(baseUrl + "Contents-" + architecture + compressionFormat)

                for component in self._components:
                    for architecture in self._architectures:
                        if Settings.Contents():
                            for compressionFormat in compressionFormats:
                                indexes.append(f"{baseUrl}{component}/Contents-{architecture}{compressionFormat}")

                        indexes.append(f"{baseUrl}{component}/binary-{architecture}/Release")

                        for compressionFormat in compressionFormats:
                            indexes.append(baseUrl + component + "/binary-" + architecture + "/Packages" + compressionFormat)
                            self._indexCollection.Add(component, architecture, baseUrl + component + "/binary-" + architecture + "/Packages" + compressionFormat)
                            indexes.append(baseUrl + component + "/cnf/Commands-" + architecture + compressionFormat)
                            indexes.append(baseUrl + component + "/i18n/cnf/Commands-" + architecture + compressionFormat)

                    indexes.append(baseUrl + component + "/i18n/Index")
        elif self._sourceType == SourceType.Src:
            # Source Files
            if self._components:
                for component in self._components:
                    indexes.append(baseUrl + component + "/source/Release")
                    for compressionFormat in compressionFormats:
                        indexes.append(baseUrl + component + "/source/Sources" + compressionFormat)
                        self._indexCollection.Add(component, self._architectures[0], baseUrl + component + "/source/Sources" + compressionFormat)

        self._indexCollection.DetermineCurrentTimestamps()

        return indexes

    def Timestamp(self):
        self._indexCollection.DetermineDownloadTimestamps()

    def GetReleaseFiles(self, modified: bool) -> list:
        """Get a list of all Release files for the Source."""

        if modified:
            return self._indexCollection.Files
        else:
            return self._indexCollection.UnmodifiedFiles

    def GetTranslationIndexes(self) -> list:
        """Get a list of all TranslationIndexes for the Source if it represents a deb mirror."""
        if self._sourceType != SourceType.Bin:
            return []

        baseUrl = self._uri + "/dists/" + self._distribution + "/"

        translationIndexes = []

        for component in self._components:
            translationIndexes += self.__ProcessTranslationIndex(baseUrl, component)

        return translationIndexes

    def GetDep11Files(self) -> list:
        """Get a list of all TranslationIndexes for the Source if it represents a deb mirror."""
        if self._sourceType != SourceType.Bin:
            return []

        baseUrl = self._uri + "/dists/" + self._distribution + "/"
        releaseUri = baseUrl + "Release"
        releasePath = Settings.SkelPath() + "/" + SanitiseUri(releaseUri)

        dep11Files = []

        for component in self._components:
            dep11Files += self.__ProcessLine(releasePath, IndexType.Dep11, baseUrl, "", component)

        return dep11Files

    def __ProcessTranslationIndex(self, url: str, component: str) -> list:
        """Extract all Translation files from the /dists/$DIST/$COMPONENT/i18n/Index file.

           Falls back to parsing /dists/$DIST/Release if /i18n/Index is not found.
        """

        baseUri = url + component + "/i18n/"
        indexUri = baseUri + "Index"
        indexPath = Settings.SkelPath() + "/" + SanitiseUri(indexUri)

        if not os.path.isfile(indexPath):
            releaseUri = url + "Release"
            releasePath = Settings.SkelPath() + "/" + SanitiseUri(releaseUri)
            return self.__ProcessLine(releasePath, IndexType.Release, url, "", component)
        else:
            return self.__ProcessLine(indexPath, IndexType.Index, indexUri, baseUri, "")

    def __ProcessLine(self, file: str, indexType: IndexType, indexUri: str, baseUri: str = "", component: str = "") -> list:
        """Parses an Index file for all filenames."""
        checksums = False

        indexes = []

        with open(file) as f:
            for line in f:
                if "SHA256:" in line or "SHA1:" in line or "MD5Sum:" in line:
                    checksumType = line
                    checksumType = checksumType.replace(":", "").strip()

                if checksums:
                    if re.search("^ +(.*)$", line):
                        parts = list(filter(None, line.split(" ")))

                        # parts[0] = sha1
                        # parts[1] = size
                        # parts[2] = filename

                        if not len(parts) == 3:
                            logger.warn(f"Malformed checksum line '{line}' in {indexUri}")
                            continue

                        checksum = parts[0].strip()
                        filename = parts[2].rstrip()

                        if indexType == IndexType.Release:
                            if re.match(rf"{component}/i18n/Translation-[^./]*\.(gz|bz2|xz)$", filename):
                                indexes.append(indexUri + filename)
                                if Settings.ByHash():
                                    indexes.append(f"{indexUri}{component}/i18n/by-hash/{checksumType}/{checksum}")
                        elif indexType == IndexType.Dep11:
                            for arch in self._architectures:
                                if re.match(rf"{component}/dep11/(Components-{arch}\.yml|icons-[^./]+\.tar)\.(gz|bz2|xz)$", filename):
                                    indexes.append(indexUri + filename)
                            if Settings.ByHash():
                                indexes.append(f"{indexUri}{component}/dep11/by-hash/{checksumType}/{checksum}")
                        else:
                            indexes.append(baseUri + filename)
                    else:
                        checksums = False
                else:
                    checksums = "SHA256:" in line or "SHA1:" in line or "MD5Sum:" in line

        return indexes

    @property
    def SourceType(self) -> SourceType:
        """Gets the type of Source this object represents."""
        return self._sourceType

    @property
    def Uri(self) -> str:
        """Gets the Uri of the Source."""
        return self._uri

    @property
    def Distribution(self) -> str:
        """Gets the Distribution of the Source."""
        return self._distribution

    @property
    def Components(self) -> list:
        """Gets the Components of the Source."""
        return self._components

    @property
    def Architectures(self) -> list:
        """Gets the Architectures of the Source."""
        return self._architectures

    @property
    def Clean(self) -> bool:
        """Gets whether the resulting directory should be cleaned."""
        return self._clean

    @Clean.setter
    def Clean(self, value: bool):
        """Sets whether the resulting directory should be cleaned."""
        self._clean = value

    @property
    def Modified(self) -> bool:
        """Get whether any of the files in this source have been modified"""
        return len(self._indexCollection.Files) > 0

class Timestamp:
    def __init__(self):
        self._currentTimestamp = 0.0
        self._downloadedTimestamp = 0.0

    @property
    def Current(self) -> float:
        return self._currentTimestamp

    @Current.setter
    def Current(self, timestamp: float):
        self._currentTimestamp = timestamp

    @property
    def Download(self) -> float:
        return self._downloadedTimestamp

    @Download.setter
    def Download(self, timestamp: float):
        self._downloadedTimestamp = timestamp

    @property
    def Modified(self) -> bool:
        return self._currentTimestamp != self._downloadedTimestamp

class IndexCollection:
    def __init__(self, components: list, architectures: list):
        self._indexCollection = collections.defaultdict(lambda : collections.defaultdict(dict)) # type: dict[str, dict[str, dict[str, Timestamp]]] # For each component, each architecture, for each file, timestamp

        # Initialise the Index Collection
        for component in components:
            for architecture in architectures:
                self._indexCollection[component][architecture] = dict()

    def Add(self, component: str, architecture: str, file: str):
        self._indexCollection[component][architecture][SanitiseUri(file)] = Timestamp()

    def DetermineCurrentTimestamps(self):
        logger.debug("Getting timestamps of current files in Skel (if available)")
        # Now we have built our index collection, gather timestamps for all the files (that exist)
        for component in self._indexCollection:
            for architecture in self._indexCollection[component]:
                for file in self._indexCollection[component][architecture]:
                    if os.path.isfile(f"{Settings.SkelPath()}/{file}"):
                        self._indexCollection[component][architecture][file].Current = os.path.getmtime(Path(f"{Settings.SkelPath()}/{SanitiseUri(file)}"))
                        logger.debug(f"\tCurrent: [{component}] [{architecture}] [{file}]: {self._indexCollection[component][architecture][file].Current}")

    def DetermineDownloadTimestamps(self):
        logger.debug("Getting timestamps of downloaded files in Skel")
        removables = collections.defaultdict(dict) # type: dict[str, dict[str, list[str]]]
        for component in self._indexCollection:
            for architecture in self._indexCollection[component]:
                removables[component][architecture] = list()

        for component in self._indexCollection:
            for architecture in self._indexCollection[component]:
                for file in self._indexCollection[component][architecture]:
                    if os.path.isfile(f"{Settings.SkelPath()}/{file}"):
                        self._indexCollection[component][architecture][file].Download = os.path.getmtime(Path(f"{Settings.SkelPath()}/{SanitiseUri(file)}"))
                        logger.debug(f"\tDownload: [{component}] [{architecture}] [{file}]: {self._indexCollection[component][architecture][file].Download}")
                    else:
                        # File does not exist after download, therefore it does not exist, and can marked for removal.
                        removables[component][architecture].append(file)
                        logger.debug(f"\tMarked for removal (does not exist): [{component}] [{architecture}] [{file}]: ")

        for component in removables:
            for architecture in removables[component]:
                for file in removables[component][architecture]:
                    del self._indexCollection[component][architecture][file]

    @property
    def Files(self) -> list:
        files = [] # type: list[str]

        for component in self._indexCollection:
            for architecture in self._indexCollection[component]:
                for file in self._indexCollection[component][architecture]:
                    if self._indexCollection[component][architecture][file].Modified or Settings.Force():
                        filename, _ = os.path.splitext(file)
                        files.append(filename)

        return list(set(files)) # Ensure uniqueness

    @property
    def UnmodifiedFiles(self) -> list:
        files = [] # type: list[str]

        for component in self._indexCollection:
            for architecture in self._indexCollection[component]:
                for file in self._indexCollection[component][architecture]:
                    if not self._indexCollection[component][architecture][file].Modified:
                        filename, _ = os.path.splitext(file)
                        files.append(filename)

        return list(set(files)) # Ensure uniqueness

@dataclass
class Downloader:
    """Downloads a list of files."""
    @staticmethod
    def Init():
        """Setup filelock for quieter logging and handling of lock files (unix)."""

        # Quieten filelock's logger
        filelock.logger().setLevel(logging.CRITICAL)

        # filelock does not delete releasd lock files on Unix due
        # to potential race conditions in the event of multiple
        # programs trying to lock the file.
        # Refrapt only uses them to track whether a file was fully
        # downloaded or not in the event of interruption, so we
        # can cleanup the files now.
        for file in os.listdir(Settings.VarPath()):
            if ".lock" in file:
                os.remove(f"{Settings.VarPath()}/{file}")

    @staticmethod
    def Download(urls: list, kind: UrlType):
        """Download a list of files of a specific type"""
        if not urls:
            logger.info("No files to download")
            return

        arguments = Downloader.CustomArguments()

        logger.info(f"Downloading {len(urls)} {kind.name} files...")

        with multiprocessing.Pool(Settings.Threads()) as pool:
            downloadFunc = partial(Downloader.DownloadUrlsProcess, kind=kind.name, args=arguments, logPath=Settings.VarPath(), rateLimit=Settings.LimitRate())
            for _ in tqdm.tqdm(pool.imap_unordered(downloadFunc, urls), total=len(urls), unit=" file"):
                pass

    @staticmethod
    def DownloadUrlsProcess(url: str, kind: str, args: list, logPath: str, rateLimit: str):
        """Worker method for downloading a particular Url, used in multiprocessing."""
        p = multiprocessing.current_process()

        baseCommand   = "wget --no-cache -N"
        rateLimit     = f"--limit-rate={rateLimit}"
        retries       = "-t 5"
        recursiveOpts = "-r -l inf"
        logFile       = f"-a {logPath}/{kind}-log.{p._identity[0]}"

        filename = f"{logPath}/Download-lock.{p._identity[0]}"

        with filelock.FileLock(f"{filename}.lock"):
            with open(filename, "w") as f:
                f.write(url)

            os.system(f"{baseCommand} {rateLimit} {retries} {recursiveOpts} {logFile} {url} {args}")

            os.remove(filename)

    @staticmethod
    def CustomArguments() -> list:
        """Creates custom Wget arguments based on the Settings provided."""
        arguments = []

        if Settings.AuthNoChallege():
            arguments.append("--auth-no-challenge")
        if Settings.NoCheckCertificate():
            arguments.append("--no-check-certificate")
        if Settings.Unlink():
            arguments.append("--unlink")

        if Settings.Certificate():
            arguments.append(f"--certificate={Settings.Certificate()}")
        if Settings.CaCertificate():
            arguments.append(f"--ca-certificate={Settings.CaCertificate()}")
        if Settings.PrivateKey():
            arguments.append(f"--privateKey={Settings.PrivateKey()}")

        if Settings.UseProxy():
            arguments.append("-e use_proxy=yes")

            if Settings.HttpProxy():
                arguments.append("-e http_proxy=" + Settings.HttpProxy())
            if Settings.HttpsProxy():
                arguments.append("-e https_proxy=" + Settings.HttpsProxy())
            if Settings.ProxyUser():
                arguments.append("-e proxy_user=" + Settings.ProxyUser())
            if Settings.ProxyPassword():
                arguments.append("-e proxy_password=" + Settings.ProxyPassword())

        return arguments

class Index:
    """Represents an Index file."""
    def __init__(self, path: str):
        """Initialise an Index file with a path."""
        self._path = path

    def Read(self):
        """Read and decode the contents of the file."""
        contents = []
        self._lines = []

        with open(self._path, "rb") as f:
            contents = f.readlines()

        for line in contents:
            self._lines.append(line.decode().rstrip())

    def GetPackages(self) -> list:
        """Get a list of all Packages listed in the file."""
        packages = []    # type: list[dict[str,str]]
        package = dict() # type: dict[str,str]

        keywords = ["Filename", "MD5sum", "SHA1", "SHA256", "Size", "Files", "Directory"]

        key = None

        for line in self._lines:
            if not line:
                packages.append(package)
                package = dict()
            else:
                match = re.search(r"^([\w\-]+:)", line)
                if not match and key:
                    # Value continues on next line, append data
                    package[key] += f"\n{line.strip()}"
                else:
                    key = line.split(":")[0]
                    if key in keywords:
                        value = line.split(":")[1].strip()
                        package[key] = value
                    else:
                        # Ignore, we don't need it
                        key = None

        return packages

class LogFilter(object):
    """Class to provide filtering for logging.

       The Level passed to this class will define the minimum
       log level that is allowed by logger.
    """
    def __init__(self, level):
        """Initialise the filter level."""
        self.__level = level

    def filter(self, logRecord):
        """Return whether the Record is covered by a filter or not."""
        return logRecord.levelno >= self.__level
