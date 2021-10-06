"""
Parses the output from an Amazon Transcribe job into turn-by-turn
speech segments with sentiment analysis scores from Amazon Comprehend
"""

from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
import pcaconfiguration as cf
from pcakendrasearch import prepare_transcript, put_kendra_document
import subprocess
import copy
import re
import json
import csv
import boto3
import sys
import time

# Sentiment helpers
MIN_SENTIMENT_LENGTH = 8
NLP_THROTTLE_RETRIES = 1

# PII and other Markers
PII_PLACEHOLDER = "[PII]"
PII_PLACEHOLDER_MASK = "*" * len(PII_PLACEHOLDER)
TMP_DIR = "/tmp"


class SpeechSegment:
    """ Class to hold information about a single speech segment """
    def __init__(self):
        self.segmentStartTime = 0.0
        self.segmentEndTime = 0.0
        self.segmentSpeaker = ""
        self.segmentText = ""
        self.segmentConfidence = []
        self.segmentSentimentScore = -1.0    # -1.0 => no sentiment calculated
        self.segmentPositive = 0.0
        self.segmentNegative = 0.0
        self.segmentIsPositive = False
        self.segmentIsNegative = False
        self.segmentAllSentiments = []
        self.segmentCustomEntities = []
        self.segmentLoudnessScores = []
        self.segmentInterruption = False
        self.segmentIssuesDetected = []


class TranscribeParser:

    def __init__(self, minSentimentPos, minSentimentNeg, customEntityEndpoint):
        self.min_sentiment_positive = minSentimentPos
        self.min_sentiment_negative = minSentimentNeg
        self.transcribeJobInfo = ""
        self.conversationLanguageCode = ""
        self.comprehendLanguageCode = ""
        self.guid = ""
        self.agent = ""
        self.conversationTime = ""
        self.conversationLocation = ""
        self.speechSegmentList = []
        self.headerEntityDict = {}
        self.numWordsParsed = 0
        self.cummulativeWordAccuracy = 0.0
        self.maxSpeakerIndex = 0
        self.customEntityEndpointName = customEntityEndpoint
        self.customEntityEndpointARN = ""
        self.simpleEntityMap = {}
        self.matchedSimpleEntities = {}
        self.audioPlaybackUri = ""
        self.duration = 0.0
        self.transcript_uri = ""
        self.api_mode = cf.API_STANDARD
        self.analytics_channel_map = {}

        cf.loadConfiguration()

        # Check the model exists - if now we may use simple file entity detection instead
        if self.customEntityEndpointName != "":
            # Get the ARN for our classifier endpoint, getting out quickly if there
            # isn't one defined or if we can't find the one that is defined
            comprehendClient = boto3.client("comprehend")
            recognizerList = comprehendClient.list_endpoints()
            recognizer = list(filter(lambda x: x["EndpointArn"].endswith(self.customEntityEndpointName),
                                     recognizerList["EndpointPropertiesList"]))

            # Only use it if it exists (!) and is IN_SERVICE
            if (recognizer == []) or (recognizer[0]["Status"] != "IN_SERVICE"):
                # Doesn't exist, so ignore the config
                self.customEntityEndpointName = ""
            else:
                self.customEntityEndpointARN = recognizer[0]["EndpointArn"]

        # Set flag to say if we could do simple entities
        self.simpleEntityMatchingUsed = (self.customEntityEndpointARN == "") and \
                                        (cf.appConfig[cf.CONF_ENTITY_FILE] != "")


    def generateSpeakerSentimentTrend(self, speaker, spkNum):
        '''
        Generates and returns a sentiment trend block for this speaker

        {
          "Speaker": "string",
          "AverageSentiment": "float",
          "SentimentChange": "float"
        }
        '''
        speakerTrend = {}
        speakerTrend["Speaker"] = speaker

        speakerTurns = 0
        sumSentiment = 0.0
        firstSentiment = 0.0
        finalSentiment = 0.0
        for segment in self.speechSegmentList:
            if segment.segmentSpeaker == speaker:
                # Increment our counter for number of speaker turns and update the last turn score
                speakerTurns += 1

                if segment.segmentIsPositive or segment.segmentIsNegative:
                    # Only really interested in Positive/Negative turns for the stats.  We need to
                    # average out the calls between +/- 1, so we sum each turn as follows:
                    # ([sentiment] - [sentimentBase]) / (1 - [sentimentBase])
                    # with the answer positive/negative based on the sentiment.  We rebase as we have
                    # thresholds to declare turns as pos/neg, so might be in the range 0.30-1.00. but
                    # Need this changed to 0.00-1.00
                    if segment.segmentIsPositive:
                        sentimentBase = self.min_sentiment_positive
                        signModifier = 1.0
                    else:
                        sentimentBase = self.min_sentiment_negative
                        signModifier = -1.0

                    # Calculate score and add it to our total
                    turnScore = signModifier * ((segment.segmentSentimentScore - sentimentBase) / (1.0 - sentimentBase))
                    sumSentiment += turnScore

                    # Assist to first-turn score if this is us, and update the last-turn
                    # score, as we dont' know if this is the last turn for this speaker
                    if speakerTurns == 1:
                        firstSentiment = turnScore
                    finalSentiment = turnScore
                else:
                    finalSentiment = 0.0

        # Log our trends for this speaker
        speakerTrend["SentimentChange"] = finalSentiment - firstSentiment
        speakerTrend["AverageSentiment"] = sumSentiment / max(speakerTurns, 1)

        return speakerTrend

    def createOutputConversationAnalytics(self):
        '''
        Generates some conversation-level analytics for this document, which includes information
        about the call, speaker labels, sentiment trends and entities
        '''
        resultsHeaderInfo = {}

        # Basic information.  Note, we expect the input stream processing mechanism
        # to set the conversation time - if it is not set then we have no choice
        # but to default this to the current processing time.
        resultsHeaderInfo["GUID"] = self.guid
        resultsHeaderInfo["Agent"] = self.agent
        resultsHeaderInfo["ConversationTime"] = self.conversationTime
        resultsHeaderInfo["ConversationLocation"] = self.conversationLocation
        resultsHeaderInfo["ProcessTime"] = str(datetime.now())
        resultsHeaderInfo["LanguageCode"] = self.conversationLanguageCode
        resultsHeaderInfo["Duration"] = str(self.duration)
        if self.conversationTime == "":
            resultsHeaderInfo["ConversationTime"] = resultsHeaderInfo["ProcessTime"]

        # Build up a list of speaker labels from the config; note that if we
        # have more speakers than configured then we still return something
        speakerLabels = []

        # Standard Transcribe - look them up in the order in the config
        if self.api_mode == cf.API_STANDARD:
            for speaker in range(self.maxSpeakerIndex + 1):
                next_label = {}
                next_label["Speaker"] = "spk_" + str(speaker)
                try:
                    next_label["DisplayText"] = cf.appConfig[cf.CONF_SPEAKER_NAMES][speaker]
                except:
                    next_label["DisplayText"] = "Unknown-" + str(speaker)
                speakerLabels.append(next_label)
        # Analytics is more prescriptive - they're defined in the call results
        elif self.api_mode == cf.API_ANALYTICS:
            for speaker in self.analytics_channel_map:
                next_label = {}
                next_label["Speaker"] = "spk_" + str(self.analytics_channel_map[speaker])
                next_label["DisplayText"] = speaker.title()
                speakerLabels.append(next_label)
        resultsHeaderInfo["SpeakerLabels"] = speakerLabels

        # Sentiment Trends
        sentimentTrends = []
        for speaker in range(self.maxSpeakerIndex + 1):
            sentimentTrends.append(self.generateSpeakerSentimentTrend("spk_" + str(speaker), speaker))
        resultsHeaderInfo["SentimentTrends"] = sentimentTrends

        # Detected custom entity summaries next
        customEntityList = []
        for entity in self.headerEntityDict:
            nextEntity = {}
            nextEntity['Name'] = entity
            nextEntity['Count'] = len(self.headerEntityDict[entity])
            nextEntity['Values'] = self.headerEntityDict[entity]
            customEntityList.append(nextEntity)
        resultsHeaderInfo["CustomEntities"] = customEntityList

        # Decide which source information block to add - only one for now
        transcribeSourceInfo = {}
        transcribeSourceInfo["TranscribeJobInfo"] = self.createOutputTranscribeJobInfo()
        sourceInfo = []
        sourceInfo.append(transcribeSourceInfo)
        resultsHeaderInfo["SourceInformation"] = sourceInfo

        # Add on any file-based entity used
        if self.simpleEntityMatchingUsed:
            resultsHeaderInfo["EntityRecognizerName"] = cf.appConfig[cf.CONF_ENTITY_FILE]
        elif self.customEntityEndpointName != "":
            resultsHeaderInfo["EntityRecognizerName"] = self.customEntityEndpointName

        return resultsHeaderInfo

    def createOutputTranscribeJobInfo(self):
        '''
        "TranscribeJobInfo": {
            "TranscriptionJobName": "string",
            "TranscribeApiType": "string",
            "CompletionTime": "string",
            "VocabularyName": "string",
            "MediaFormat": "string",
            "MediaSampleRateHertz": "integer",
            "MediaFileUri": "string",
            "MediaOriginalUri": "string",
            "ChannelIdentification": "boolean",
            "AverageAccuracy": "float"
         }
        '''
        transcribeJobInfo = {}

        # Some fields we pick off the basic job info
        transcribeJobInfo["TranscribeApiType"] = self.api_mode
        transcribeJobInfo["CompletionTime"] = str(self.transcribeJobInfo["CompletionTime"])
        transcribeJobInfo["MediaFormat"] = self.transcribeJobInfo["MediaFormat"]
        transcribeJobInfo["MediaSampleRateHertz"] = self.transcribeJobInfo["MediaSampleRateHertz"]
        transcribeJobInfo["MediaOriginalUri"] = self.transcribeJobInfo["Media"]["MediaFileUri"]
        transcribeJobInfo["AverageAccuracy"] = self.cummulativeWordAccuracy / max(float(self.numWordsParsed), 1.0)

        # Did we create an MP3 output file?  If so then use it for playback rather than the original
        if self.audioPlaybackUri != "":
            transcribeJobInfo["MediaFileUri"] = self.audioPlaybackUri
        else:
            transcribeJobInfo["MediaFileUri"] = transcribeJobInfo["MediaOriginalUri"]

        # Vocabulary name is optional
        if "VocabularyName" in self.transcribeJobInfo["Settings"]:
            transcribeJobInfo["VocabularyName"] = self.transcribeJobInfo["Settings"]["VocabularyName"]

        # Vocabulary filter is optional
        if "VocabularyFilterName" in self.transcribeJobInfo["Settings"]:
            vocab_filter = self.transcribeJobInfo["Settings"]["VocabularyFilterName"]
            vocab_method = self.transcribeJobInfo["Settings"]["VocabularyFilterMethod"]
            transcribeJobInfo["VocabularyFilter"] = vocab_filter + " [" + vocab_method + "]"

        # Some fields are different in the job-status depending upon which API we were using
        if self.api_mode == cf.API_ANALYTICS:
            transcribeJobInfo["TranscriptionJobName"] = self.transcribeJobInfo["CallAnalyticsJobName"]
            transcribeJobInfo["ChannelIdentification"] = 1
        else:
            transcribeJobInfo["TranscriptionJobName"] = self.transcribeJobInfo["TranscriptionJobName"]
            transcribeJobInfo["ChannelIdentification"] = int(self.transcribeJobInfo["Settings"]["ChannelIdentification"])

        return transcribeJobInfo

    def createOutputSpeechSegments(self):
        '''
        Creates a list of speech segments for this conversation, including custom entities

         "SpeechSegments": [
            {
              "SegmentStartTime": "float",
              "SegmentEndTime": "float",
              "SegmentSpeaker": "string",
              "OriginalText": "string",
              "DisplayText": "string",
              "TextEdited": "boolean",
              "SentimentIsPositive": "boolean",
              "SentimentIsNegative": "boolean",
              "SentimentScore": "float",
              "BaseSentimentScores": {
                "Positive": "float",
                "Negative": "float",
                "Neutral": "float",
                "Mixed": "float"
              },
              "EntitiesDetected": [
                {
                  "Type": "string",
                  "Text": "string",
                  "BeginOffset": "integer",
                  "EndOffset": "integer",
                  "Score": "float"
                }
              ],
              "WordConfidence": [
                {
                  "Text": "string",
                  "Confidence": "float",
                  "StartTime": "float",
                  "EndTime": "float"
                }
              ]
            }
          ]
          '''
        speechSegments = []

        # Loop through each of our speech segments
        # for segment in self.speechSegmentList:
        for segment in self.speechSegmentList:
            nextSegment = {}

            # Pick everything off our structures
            nextSegment["SegmentStartTime"] = segment.segmentStartTime
            nextSegment["SegmentEndTime"] = segment.segmentEndTime
            nextSegment["SegmentSpeaker"] = segment.segmentSpeaker
            nextSegment["OriginalText"] = segment.segmentText
            nextSegment["DisplayText"] = segment.segmentText
            nextSegment["TextEdited"] = 0
            nextSegment["SentimentIsPositive"] = int(segment.segmentIsPositive)
            nextSegment["SentimentIsNegative"] = int(segment.segmentIsNegative)
            nextSegment["SentimentScore"] = segment.segmentSentimentScore
            nextSegment["BaseSentimentScores"] = segment.segmentAllSentiments
            nextSegment["EntitiesDetected"] = segment.segmentCustomEntities
            nextSegment["WordConfidence"] = segment.segmentConfidence

            # Add what we have to the full list
            speechSegments.append(nextSegment)

        return speechSegments

    def outputAsJSON(self):
        '''
        {
            "ConversationAnalytics": { },
            "SpeechSegments": [ ]
        }
        '''
        outputJson = {}
        outputJson["ConversationAnalytics"] = self.createOutputConversationAnalytics()
        outputJson["SpeechSegments"] = self.createOutputSpeechSegments()

        return outputJson

    def mergeSpeakerSegments(self, inputSegmentList):
        """
        Merges together two adjacent speaker segments if (a) the speaker is
        the same, and (b) if the gap between them is less than 3 seconds
        """
        outputSegmentList = []
        lastSpeaker = ""
        lastSegment = None

        # Step through each of our defined speaker segments
        for segment in inputSegmentList:
            if (segment.segmentSpeaker != lastSpeaker) or ((segment.segmentStartTime - lastSegment.segmentEndTime) >= 3.0):
                # Simple case - speaker change or > 3.0 second gap means new output segment
                outputSegmentList.append(segment)

                # This is now our base segment moving forward
                lastSpeaker = segment.segmentSpeaker
                lastSegment = segment
            else:
                # Same speaker, short time, need to copy this info to the last one
                lastSegment.segmentEndTime = segment.segmentEndTime
                lastSegment.segmentText += " " + segment.segmentText
                segment.segmentConfidence[0]["Text"] = " " + segment.segmentConfidence[0]["Text"]
                for wordConfidence in segment.segmentConfidence:
                    lastSegment.segmentConfidence.append(wordConfidence)

        return outputSegmentList

    def updateHeaderEntityCount(self, entityType, entityValue):
        """
        Updates the header-level entity structure with the given tuple, but duplicates are not added
        """
        # Ensure we have an entry in our collection for this key
        if entityType not in self.headerEntityDict:
            self.headerEntityDict[entityType] = []

        # If we don't already have this tuple then add it to the header
        keyDetails = self.headerEntityDict[entityType]
        if not entityValue in keyDetails:
            keyDetails.append(entityValue)
            self.headerEntityDict[entityType] = keyDetails

    def extractEntitiesFromLine(self, entityLine, speechSegment, typeFilter):
        """
        Takes a speech segment and an entity line from Comprehend - standard or custom models - and
        if the entity type is in our input type filter (or is blank) then add it to the transcript
        """
        if float(entityLine['Score']) >= cf.appConfig[cf.CONF_ENTITYCONF]:
            entityType = entityLine['Type']

            # If we have a type filter then ensure we match it before adding the entry
            if (typeFilter == []) or (entityType in typeFilter):

                # Update our header entry
                self.updateHeaderEntityCount(entityType, entityLine["Text"])

                # Now do the same with the SpeechSegment, but append the full details
                speechSegment.segmentCustomEntities.append(entityLine)

    def setComprehendLanguageCode(self, transcribeLangCode):
        '''
        Based upon the language defined by the input stream set the best-match language code for Comprehend to use
        for this conversation.  It is "best-match" as Comprehend can model in EN, but has no differentiation between
        EN-US and EN-GB.  If we cannot determine a language to use then we cannot use Comprehend standard models
        '''
        targetLangModel = ""
        self.conversationLanguageCode = transcribeLangCode

        try:
            for checkLangCode in cf.appConfig[cf.CONF_COMP_LANGS]:
                if transcribeLangCode.startswith(checkLangCode):
                    targetLangModel = checkLangCode
                    break
        except:
            # If anything fails - e.g. no language  string - then we have no language for Comprehend
            pass

        self.comprehendLanguageCode = targetLangModel

    def comprehendSingleSentiment(self, text, client):
        """
        Perform sentiment analysis, but try and avert throttling by trying one more time if this exceptions.
        It is not a replacement for limit increases, but will help limit failures if usage suddenly grows
        """
        sentimentResponse = {}
        counter = 0
        while sentimentResponse == {}:
            try:
                sentimentResponse = client.detect_sentiment(Text=text, LanguageCode=self.comprehendLanguageCode)
            except Exception as e:
                if counter < NLP_THROTTLE_RETRIES:
                    counter += 1
                    time.sleep(3)
                else:
                    raise e

        return sentimentResponse

    def comprehendSingleEntity(self, text, client):
        """
        Perform entity analysis, but try and avert throttling by trying one more time if this exceptions.
        It is not a replacement for limit increases, but will help limit failures if usage suddenly grows
        """
        entityResponse = {}
        counter = 0
        while entityResponse == {}:
            try:
                entityResponse = client.detect_entities(Text=text, LanguageCode=self.comprehendLanguageCode)
            except Exception as e:
                if counter < NLP_THROTTLE_RETRIES:
                    counter += 1
                    time.sleep(3)
                else:
                    raise e

        return entityResponse

    def extract_nlp(self, segment_list):
        """
        Generates sentiment per speech segment, inserting the results into the input list.
        If we had no valid language for Comprehend to use then we use Neutral for everything.
        It also extracts standard LOCATION entities, and calls any custom entity recognition
        model that has been configured for that language
        """
        client = boto3.client("comprehend")

        # Setup some sentiment blocks - used when we have no Comprehend
        # language or where we need "something" for Call Analytics
        sentiment_set_neutral = {'Positive': 0.0, 'Negative': 0.0, 'Neutral': 1.0, 'Mixed': 0.0}
        sentiment_set_positive = {'Positive': 1.0, 'Negative': 0.0, 'Neutral': 0.0, 'Mixed': 0.0}
        sentiment_set_negative = {'Positive': 0.0, 'Negative': 1.0, 'Neutral': 0.0, 'Mixed': 0.0}

        # Go through each of our segments
        for next_segment in segment_list:
            if len(next_segment.segmentText) >= MIN_SENTIMENT_LENGTH:
                nextText = next_segment.segmentText

                # First, set the sentiment scores in the transcript.  In Call Analytics mode
                # we already have a sentiment marker (+ve/-ve) per turn of the transcript
                if self.api_mode == cf.API_ANALYTICS:
                    # Just set some fake scores against the line to match the sentiment type
                    if next_segment.segmentIsPositive:
                        next_segment.segmentAllSentiments = sentiment_set_positive
                    elif next_segment.segmentIsNegative:
                        next_segment.segmentAllSentiments = sentiment_set_negative
                    else:
                        next_segment.segmentAllSentiments = sentiment_set_neutral
                # Standard Transcribe requires us to use Comprehend
                else:
                    # We can only use Comprehend if we have a language code
                    if self.comprehendLanguageCode == "":
                        # We had no language - use default neutral sentiment scores
                        next_segment.segmentAllSentiments = sentiment_set_neutral
                        next_segment.segmentPositive = 0.0
                        next_segment.segmentNegative = 0.0
                    else:
                        # For Standard Transcribe we need to set the sentiment marker based on score thresholds
                        sentimentResponse = self.comprehendSingleSentiment(nextText, client)
                        positiveBase = sentimentResponse["SentimentScore"]["Positive"]
                        negativeBase = sentimentResponse["SentimentScore"]["Negative"]

                        # If we're over the NEGATIVE threshold then we're negative
                        if negativeBase >= self.min_sentiment_negative:
                            next_segment.segmentSentiment = "Negative"
                            next_segment.segmentIsNegative = True
                            next_segment.segmentSentimentScore = negativeBase
                        # Else if we're over the POSITIVE threshold then we're positive,
                        # otherwise we're either MIXED or NEUTRAL and we don't really care
                        elif positiveBase >= self.min_sentiment_positive:
                            next_segment.segmentSentiment = "Positive"
                            next_segment.segmentIsPositive = True
                            next_segment.segmentSentimentScore = positiveBase

                        # Store all of the original sentiments for future use
                        next_segment.segmentAllSentiments = sentimentResponse["SentimentScore"]
                        next_segment.segmentPositive = positiveBase
                        next_segment.segmentNegative = negativeBase

                # If we have a language model then extract entities via Comprehend,
                # and the same methodology is used for all of the Transcribe modes
                if self.comprehendLanguageCode != "":
                    # Get sentiment and standard entity detection from Comprehend
                    pii_masked_text = nextText.replace(PII_PLACEHOLDER, PII_PLACEHOLDER_MASK)
                    entity_response = self.comprehendSingleEntity(pii_masked_text, client)

                    # Filter for desired entity types
                    for detected_entity in entity_response["Entities"]:
                        self.extractEntitiesFromLine(detected_entity, next_segment, cf.appConfig[cf.CONF_ENTITY_TYPES])

                    # Now do the same for any entities we can find in a custom model.  At the
                    # time of writing, Custom Entity models in Comprehend are ENGLISH ONLY
                    if (self.customEntityEndpointARN != "") and (self.comprehendLanguageCode == "en"):
                        # Call the custom model and insert
                        custom_entity_response = client.detect_entities(Text=pii_masked_text,
                                                                        EndpointArn=self.customEntityEndpointARN)
                        for detected_entity in custom_entity_response["Entities"]:
                            self.extractEntitiesFromLine(detected_entity, next_segment, [])

    def generateSpeakerLabel(self, standard_ts_speaker="", analytics_ts_speaker=""):
        '''
        Takes the Transcribed-generated speaker, which could be spk_{N} or ch_{N}, and returns the label spk_{N}.
        This allows us to have a consistent label in the output JSON, which means that a header field in the
        output is able to dynamically swap the display labels.  This is needed as we cannot guarantee, especially
        with speaker-separated, who speaks first
        '''

        # Extract our speaker number
        if standard_ts_speaker != "":
            # Standard transcribe gives us ch_0 or spk_0
            index = standard_ts_speaker.find("_")
            speaker = int(standard_ts_speaker[index + 1:])
        elif (analytics_ts_speaker != "") and (self.analytics_channel_map != {}):
            # Analytics has a map of participant to channel
            speaker = self.analytics_channel_map[analytics_ts_speaker]

        # Track the maximum and return the label
        if speaker > self.maxSpeakerIndex:
            self.maxSpeakerIndex = speaker
        newLabel = "spk_" + str(speaker)
        return newLabel


    def createTurnByTurnSegments(self, transcribe_job_filename):
        """
        Creates a list of conversational turns, splitting up by speaker or if there's a noticeable pause in
        conversation.  Notes, this works differently for speaker-separated and channel-separated files. For speaker-
        the lines are already separated by speaker, so we only worry about splitting up speaker pauses of more than 3
        seconds, but for channel- we have to hunt gaps of 100ms across an entire channel, then sort segments from both
        channels, then merge any together to ensure we keep to the 3-second pause; this way means that channel- files
        are able to show interleaved speech where speakers are talking over one another.  Once all of this is done
        we inject sentiment into each segment.
        """
        speechSegmentList = []

        # Load in the JSON file for processing
        json_filepath = Path(transcribe_job_filename)
        data = json.load(open(json_filepath.absolute(), "r", encoding="utf-8"))
        is_analytics_mode = (self.api_mode == cf.API_ANALYTICS)

        # Decide on our operational mode and set the overall job language
        if is_analytics_mode:
            # We ignore speaker/channel mode on Analytics
            isChannelMode = False
            isSpeakerMode = False
        else:
            # Channel/Speaker-mode only relevant if not using analytics
            isChannelMode = self.transcribeJobInfo["Settings"]["ChannelIdentification"]
            isSpeakerMode = not isChannelMode

        lastSpeaker = ""
        lastEndTime = 0.0
        skipLeadingSpace = False
        confidenceList = []
        nextSpeechSegment = None

        # Process a Speaker-separated non-Analytics file
        if isSpeakerMode:
            # A segment is a blob of pronunciation and punctuation by an individual speaker
            for segment in data["results"]["speaker_labels"]["segments"]:

                # If there is content in the segment then pick out the time and speaker
                if len(segment["items"]) > 0:
                    # Pick out our next data
                    nextStartTime = float(segment["start_time"])
                    nextEndTime = float(segment["end_time"])
                    nextSpeaker = self.generateSpeakerLabel(standard_ts_speaker=str(segment["speaker_label"]))

                    # If we've changed speaker, or there's a 3-second gap, create a new row
                    if (nextSpeaker != lastSpeaker) or ((nextStartTime - lastEndTime) >= 3.0):
                        nextSpeechSegment = SpeechSegment()
                        speechSegmentList.append(nextSpeechSegment)
                        nextSpeechSegment.segmentStartTime = nextStartTime
                        nextSpeechSegment.segmentSpeaker = nextSpeaker
                        skipLeadingSpace = True
                        confidenceList = []
                        nextSpeechSegment.segmentConfidence = confidenceList
                    nextSpeechSegment.segmentEndTime = nextEndTime

                    # Note the speaker and end time of this segment for the next iteration
                    lastSpeaker = nextSpeaker
                    lastEndTime = nextEndTime

                    # For each word in the segment...
                    for word in segment["items"]:

                        # Get the word with the highest confidence
                        pronunciations = list(filter(lambda x: x["type"] == "pronunciation", data["results"]["items"]))
                        word_result = list(filter(lambda x: x["start_time"] == word["start_time"] and x["end_time"] == word["end_time"], pronunciations))
                        try:
                            result = sorted(word_result[-1]["alternatives"], key=lambda x: x["confidence"])[-1]
                            confidence = float(result["confidence"])
                        except:
                            result = word_result[-1]["alternatives"][0]
                            confidence = float(result["redactions"][0]["confidence"])

                        # Write the word, and a leading space if this isn't the start of the segment
                        if skipLeadingSpace:
                            skipLeadingSpace = False
                            wordToAdd = result["content"]
                        else:
                            wordToAdd = " " + result["content"]

                        # If the next item is punctuation, add it to the current word
                        try:
                            word_result_index = data["results"]["items"].index(word_result[0])
                            next_item = data["results"]["items"][word_result_index + 1]
                            if next_item["type"] == "punctuation":
                                wordToAdd += next_item["alternatives"][0]["content"]
                        except IndexError:
                            pass

                        # Add word and confidence to the segment and to our overall stats
                        nextSpeechSegment.segmentText += wordToAdd
                        confidenceList.append({"Text": wordToAdd,
                                               "Confidence": confidence,
                                               "StartTime": float(word["start_time"]),
                                               "EndTime": float(word["end_time"])})
                        self.numWordsParsed += 1
                        self.cummulativeWordAccuracy += confidence

        # Process a Channel-separated file
        elif isChannelMode:

            # A channel contains all pronunciation and punctuation from a single speaker
            for channel in data["results"]["channel_labels"]["channels"]:

                # If there is content in the channel then start processing it
                if len(channel["items"]) > 0:

                    # We have the same speaker all the way through this channel
                    nextSpeaker = self.generateSpeakerLabel(standard_ts_speaker=str(channel["channel_label"]))
                    for word in channel["items"]:
                        # Pick out our next data from a 'pronunciation'
                        if word["type"] == "pronunciation":
                            nextStartTime = float(word["start_time"])
                            nextEndTime = float(word["end_time"])

                            # If we've changed speaker, or we haven't and the
                            # pause is very small, then start a new text segment
                            if (nextSpeaker != lastSpeaker) or\
                                    ((nextSpeaker == lastSpeaker) and ((nextStartTime - lastEndTime) > 0.1)):
                                nextSpeechSegment = SpeechSegment()
                                speechSegmentList.append(nextSpeechSegment)
                                nextSpeechSegment.segmentStartTime = nextStartTime
                                nextSpeechSegment.segmentSpeaker = nextSpeaker
                                skipLeadingSpace = True
                                confidenceList = []
                                nextSpeechSegment.segmentConfidence = confidenceList
                            nextSpeechSegment.segmentEndTime = nextEndTime

                            # Note the speaker and end time of this segment for the next iteration
                            lastSpeaker = nextSpeaker
                            lastEndTime = nextEndTime

                            # Get the word with the highest confidence
                            pronunciations = list(filter(lambda x: x["type"] == "pronunciation", channel["items"]))
                            word_result = list(filter(lambda x: x["start_time"] == word["start_time"] and x["end_time"] == word["end_time"], pronunciations))
                            try:
                                result = sorted(word_result[-1]["alternatives"], key=lambda x: x["confidence"])[-1]
                                confidence = float(result["confidence"])
                            except:
                                result = word_result[-1]["alternatives"][0]
                                confidence = float(result["redactions"][0]["confidence"])

                            # Write the word, and a leading space if this isn't the start of the segment
                            if skipLeadingSpace:
                                skipLeadingSpace = False
                                wordToAdd = result["content"]
                            else:
                                wordToAdd = " " + result["content"]

                            # If the next item is punctuation, add it to the current word
                            try:
                                word_result_index = channel["items"].index(word_result[0])
                                next_item = channel["items"][word_result_index + 1]
                                if next_item["type"] == "punctuation":
                                    wordToAdd += next_item["alternatives"][0]["content"]
                            except IndexError:
                                pass

                            # Add word and confidence to the segment and to our overall stats
                            nextSpeechSegment.segmentText += wordToAdd
                            confidenceList.append({"Text": wordToAdd,
                                                   "Confidence": confidence,
                                                   "StartTime": float(word["start_time"]),
                                                   "EndTime": float(word["end_time"])})
                            self.numWordsParsed += 1
                            self.cummulativeWordAccuracy += confidence

            # Sort the segments, as they are in channel-order and not speaker-order, then
            # merge together turns from the same speaker that are very close together
            speechSegmentList = sorted(speechSegmentList, key=lambda segment: segment.segmentStartTime)
            speechSegmentList = self.mergeSpeakerSegments(speechSegmentList)

        # Process a Call Analytics file
        elif is_analytics_mode:

            # Create our speaker mapping - we need consistent output like spk_0 | spk_1
            # across all Transcribe API variants to help the UI render it all the same
            for channel_def in self.transcribeJobInfo["ChannelDefinitions"]:
                self.analytics_channel_map[channel_def["ParticipantRole"]] = channel_def["ChannelId"]

            # Lookup shortcuts
            interrupts = data["ConversationCharacteristics"]["Interruptions"]

            # Each turn has already been processed by Transcribe, so the outputs are in order
            for turn in data["Transcript"]:

                # Get our next speaker name
                nextSpeaker = self.generateSpeakerLabel(analytics_ts_speaker=turn["ParticipantRole"])

                # Setup the next speaker block
                nextSpeechSegment = SpeechSegment()
                speechSegmentList.append(nextSpeechSegment)
                nextSpeechSegment.segmentStartTime = float(turn["BeginOffsetMillis"]) / 1000.0
                nextSpeechSegment.segmentEndTime = float(turn["EndOffsetMillis"]) / 1000.0
                nextSpeechSegment.segmentSpeaker = nextSpeaker
                nextSpeechSegment.segmentText = turn["Content"]
                nextSpeechSegment.segmentLoudnessScores = turn["LoudnessScores"]
                confidenceList = []
                nextSpeechSegment.segmentConfidence = confidenceList
                skipLeadingSpace = True

                # Check if this block is within an interruption block for the speaker
                if turn["ParticipantRole"] in interrupts["InterruptionsByInterrupter"]:
                    for entry in interrupts["InterruptionsByInterrupter"][turn["ParticipantRole"]]:
                        if turn["BeginOffsetMillis"] == entry["BeginOffsetMillis"]:
                            nextSpeechSegment.segmentInterruption = True

                # Record any issues detected
                if "IssuesDetected" in turn:
                    for issue in turn["IssuesDetected"]:
                        # Grab the transcript offsets for the issue text
                        nextSpeechSegment.segmentIssuesDetected.append(issue["CharacterOffsets"])

                # Process each word in this turn
                for word in turn["Items"]:
                    # Pick out our next data from a 'pronunciation'
                    if word["Type"] == "pronunciation":
                        # Write the word, and a leading space if this isn't the start of the segment
                        if skipLeadingSpace:
                            skipLeadingSpace = False
                            wordToAdd = word["Content"]
                        else:
                            wordToAdd = " " + word["Content"]

                        # If the word is redacted then the word confidence is a bit more buried
                        if "Confidence" in word:
                            conf_score = float(word["Confidence"])
                        elif "Redaction" in word:
                            conf_score = float(word["Redaction"][0]["Confidence"])

                        # Add the word and confidence to this segment's list and to our overall stats
                        confidenceList.append({"Text": wordToAdd,
                                               "Confidence": conf_score,
                                               "StartTime": float(word["BeginOffsetMillis"]) / 1000.0,
                                               "EndTime": float(word["BeginOffsetMillis"] / 1000.0)})
                        self.numWordsParsed += 1
                        self.cummulativeWordAccuracy += conf_score

                    else:
                        # Punctuation, needs to be added to the previous word
                        last_word = nextSpeechSegment.segmentConfidence[-1]
                        last_word["Text"] = last_word["Text"] + word["Content"]

                # Tag on the sentiment - analytics has no per-turn numbers
                turn_sentiment = turn["Sentiment"]
                if turn_sentiment == "POSITIVE":
                    nextSpeechSegment.segmentIsPositive = True
                    nextSpeechSegment.segmentPositive = 1.0
                    nextSpeechSegment.segmentSentimentScore = 1.0
                elif turn_sentiment == "NEGATIVE":
                    nextSpeechSegment.segmentIsNegative = True
                    nextSpeechSegment.segmentNegative = 1.0
                    nextSpeechSegment.segmentSentimentScore = 1.0

        # Inject sentiments into the segment list
        self.extract_nlp(speechSegmentList)

        # If we ended up with any matched simple entities then insert
        # them, which we can now do as we now have the sentence order
        if self.simpleEntityMap != {}:
            self.createSimpleEntityEntries(speechSegmentList)

        # Now set the overall call duration if we actually had any speech
        if len(speechSegmentList) > 0:
            self.duration = float(speechSegmentList[-1].segmentConfidence[-1]["EndTime"])

        # Return our full turn-by-turn speaker segment list with sentiment
        return speechSegmentList

    def createSimpleEntityEntries(self, speechSegments):
        """
        Searches through the speech segments given and updates them with any of the simple entity mapping
        entries that we've found.  It also updates the line-level items.  Both methods simulate the same
        response that we'd generate if this was via Standard or Custom Comprehend Entities
        """

        # Need to check each of our speech segments for each of our entity blocks
        for nextTurn in speechSegments:
            # Now check this turn for each entity
            turnText = nextTurn.segmentText.lower()
            for nextEntity in self.simpleEntityMap:
                if nextEntity in turnText:
                    self.matchedSimpleEntities[nextEntity] = self.simpleEntityMap[nextEntity]

        # Loop through each segment looking for matches in our cut-down entity list
        for entity in self.matchedSimpleEntities:

            # Start by recording this in the header
            entityEntry = self.matchedSimpleEntities[entity]
            self.updateHeaderEntityCount(entityEntry["Type"], entityEntry["Original"])

            # Work through each segment
            # TODO Need to check we don't highlight characters in the middle of transcribed word
            # TODO Need to try and handle simple plurals (e.g. type="log" should match "logs")
            for segment in speechSegments:
                # Check if the entity text appear somewhere
                turnText = segment.segmentText.lower()
                searchFrom = 0
                index = turnText.find(entity, searchFrom)
                entityTextLength = len(entity)

                # If found then add the data in the segment, and keep going until we don't find one
                while index != -1:
                    # Got a match - add this one on, then look for another
                    # TODO if entityText is capitalised then use it, otherwise use segment text
                    nextSearchFrom = index + entityTextLength
                    newLineEntity = {}
                    newLineEntity["Score"] = 1.0
                    newLineEntity["Type"] = entityEntry["Type"]
                    newLineEntity["Text"] = entityEntry["Original"]  # TODO fix as per the above
                    newLineEntity["BeginOffset"] = index
                    newLineEntity["EndOffset"] = nextSearchFrom
                    segment.segmentCustomEntities.append(newLineEntity)

                    # Now look to see if it's repeated in this segment
                    index = turnText.find(entity, nextSearchFrom)

    def calculateTranscribeConversationTime(self, filename):
        '''
        Tries to work out the conversation time based upon patterns in the filename.
        
        The filename parsing behavior is defined in two configuration parameters:
        
        1. FilenameDatetimeRegex:
          Regular Expression (regex) used to parse call Date/Time from audio filenames. 
          Each datetime field (year, month, etc.) must be matched using a separate parenthesized group in the regex. 
          Example: the regex '(\d{2}).(\d{2}).(\d{2}).(\d{3})-(\d{2})-(\d{2})-(\d{4})' parses
          the filename: CallAudioFile-09.25.51.067-09-26-2019.wav into a value list: [09, 25, 51, 067, 09, 26, 2019]
          The next parameter, FilenameDatetimeFieldMap, maps the values to datetime field codes.
          If the filename doesn't match the regex pattern, the current time is used as the call datetime.

        2. FilenameDatetimeFieldMap:
          Space separated ordered sequence of time field codes as used by Python's datetime.strptime() function. 
          Each field code refers to a corresponding value parsed by the regex parameter filenameTimestampRegex. 
          Example: the mapping '%H %M %S %f %m %d %Y' assembles the regex values [09, 25, 51, 067, 09, 26, 2019]
          into the full datetime: '2019-09-26 09:25:51.067000'.  
          If the field map doesn't match the value list parsed by the regex, then the current time is used as the call datetime.
        '''
        regex = cf.appConfig[cf.CONF_FILENAME_DATETIME_REGEX]
        fieldmap = cf.appConfig[cf.CONF_FILENAME_DATETIME_FIELDMAP]
        print(f"INFO: Parsing datetime from filename '{filename}' using regex: '{regex}' and fieldmap: '{fieldmap}'.")
        try:
            self.conversationLocation = cf.appConfig[cf.CONF_CONVO_LOCATION]
            match = re.search(regex, filename)
            fieldstring = " ".join(match.groups())
            self.conversationTime = str(datetime.strptime(fieldstring, fieldmap))
            print(f"INFO: Assembled datetime: '{self.conversationTime}'")
        except Exception as e:
            # If everything fails system will use "now" as the datetime in UTC, which is likely wrong
            print(e)
            print(f"WARNING: Unable to parse datetime from filename. Defaulting to current system time.")
            if self.conversationLocation == "":
                self.conversationLocation = "Etc/UTC"
                
    def setGUID(self, filename):
        '''
        Tries to parse a GUID for the call from the filename using a configurable Regular Expression.
        The GUID value must be matched using one or more parenthesized groups in the regex. 
        Example: the regex '_GUID_(.*?)_' parses
        the filename: AutoRepairs1_GUID_2a602c1a-4ca3-4d37-a933-444d575c0222_AGENT_BobS_DATETIME_07.55.51.067-09-16-2021.wav 
        to extract the GUID value '2a602c1a-4ca3-4d37-a933-444d575c0222'.        
        '''
        regex = cf.appConfig[cf.CONF_FILENAME_GUID_REGEX]
        print(f"INFO: Parsing GUID from filename '{filename}' using regex: '{regex}'.")
        try:
            match = re.search(regex, filename)
            guid = " ".join(match.groups()) or 'None'
            print(f"INFO: Parsed GUID: '{guid}'")
        except:
            print(f"WARNING: Unable to parse GUID from filename {filename}, using regex: '{regex}'. Defaulting to 'None'.")
            guid='None'
        self.guid = guid

    def setAgent(self, filename):
        '''
        Tries to parse an Agent name or ID from the filename using a configurable Regular Expression.
        The AGENT value must be matched using one or more parenthesized groups in the regex. 
        Example: the regex '_AGENT_(.*?)_' parses
        the filename: AutoRepairs1_GUID_2a602c1a-4ca3-4d37-a933-444d575c0222_AGENT_BobS_DATETIME_07.55.51.067-09-16-2021.wav 
        to extract the Agent value 'BobS'.        
        '''
        regex = cf.appConfig[cf.CONF_FILENAME_AGENT_REGEX]
        print(f"INFO: Parsing AGENT from filename '{filename}' using regex: '{regex}'.")
        try:
            match = re.search(regex, filename)
            agent = " ".join(match.groups()) or 'None'
            print(f"INFO: Parsed AGENT: '{agent}'")
        except:
            print(f"WARNING: Unable to parse Agent name/ID from filename {filename}, using regex: '{regex}'. Defaulting to 'None'.")
            agent='None'
        self.agent = agent

    def loadSimpleEntityStringMap(self):
        """
        Loads in any defined simple entity map for later use - this must be a CSV file, but it will be defined
        without a language code.  We will append the Comprehend language code to the filename and use that,
        as that will give us multi-language coverage with a single file.

        Example: Configured File = entityFile.csv -> Processed File for en-US audio = entityFile-en.csv
        """

        if self.simpleEntityMatchingUsed:
            # First, need to build up the real filename to use for this language.  If we don't
            # have a language (unlikely) then just try to use the base filename as a last resort
            key = cf.appConfig[cf.CONF_ENTITY_FILE]
            if (self.comprehendLanguageCode != ""):
                key = key.split('.csv')[0] + "-" + self.comprehendLanguageCode + ".csv"

            # Then check that the language-specific mapping file actually exists
            s3 = boto3.client("s3")
            bucket = cf.appConfig[cf.CONF_SUPPORT_BUCKET]
            try:
                response = s3.get_object(Bucket=bucket, Key=key)
            except Exception as e:
                # Mapping file doesn't exist, so just quietly exit
                self.simpleEntityMatchingUsed = False
                return

            # Go download the mapping file and get it into a structure
            mapFilepath = TMP_DIR + '/' + cf.appConfig[cf.CONF_ENTITY_FILE]
            s3.download_file(bucket, key, mapFilepath)
            reader = csv.DictReader(open(mapFilepath, errors="ignore"))
            try:
                for row in reader:
                    origTerm = row.pop("Text")
                    checkTerm = origTerm.lower()
                    if not (checkTerm in self.simpleEntityMap):
                        self.simpleEntityMap[checkTerm] = { "Type": row.pop("Type"), "Original": origTerm }
            except Exception as e:
                print(e)

    def createPlaybackMP3Audio(self):
        """
        Creates and MP3-version of the audio file used in the Transcribe job, as the HTML5 <audio> playback
        controller cannot play them back if they are GSM-encoded 8Khz WAV files.  Still need to work out how
        to check for then encoding type via FFMPEG, but we do get the other info from Transcribe.

        Note - if the source audio is in a bucket that isn't the standard one, e.g. it's the alternate location,
        then the audio is always transcoded, as the UI may not have access to that bucket for playback
        """

        # Get some info on the audio file before continuing
        s3Object = urlparse(self.transcribeJobInfo["Media"]["MediaFileUri"])
        bucket = s3Object.netloc

        # 8Khz WAV or non-standard bucket audio gets converted
        if (bucket != cf.appConfig[cf.CONF_S3BUCKET_INPUT]) or\
                ((self.transcribeJobInfo["MediaFormat"] == "wav") and (self.transcribeJobInfo["MediaSampleRateHertz"] == 8000)):
            # First, we need to download the original audio file
            fileObject = s3Object.path.lstrip('/')
            inputFilename = TMP_DIR + '/' + fileObject.split('/')[-1]
            outputFilename = inputFilename.split('.wav')[0] + '.mp3'
            s3Client = boto3.client('s3')
            s3Client.download_file(bucket, fileObject, inputFilename)

            # Transform the file via FFMPEG - this will exception if not installed
            try:
                # Just convert from source to destination format
                subprocess.call(['ffmpeg', '-nostats', '-loglevel', '0', '-y', '-i', inputFilename, outputFilename], stdin=subprocess.DEVNULL)

                # Now upload the output file to the configured playback folder in the main input bucket
                s3FileKey = cf.appConfig[cf.CONF_PREFIX_MP3_PLAYBACK] + '/' + outputFilename.split('/')[-1]
                s3Client.upload_file(outputFilename, cf.appConfig[cf.CONF_S3BUCKET_INPUT], s3FileKey,
                                     ExtraArgs={'ContentType': 'audio/mp3'})
                self.audioPlaybackUri = "s3://" + cf.appConfig[cf.CONF_S3BUCKET_INPUT] + "/" + s3FileKey
            except Exception as e:
                print(e)
                print("Unable to create MP3 version of original audio file - could not find FFMPEG libraries")

    def load_transcribe_job_info(self, sf_event):
        """
        Loads in the job status for the job named in input event.  The event will inform the method which of the
        Transcribe APIs should be called (e.g. standard or call analytics).

        :param sf_event: Event info passed down from Step Functions
        :return: The job's current completion status
        """
        transcribe_client = boto3.client("transcribe")
        job_name = sf_event["jobName"]
        self.api_mode = sf_event["apiMode"]

        if self.api_mode == cf.API_STANDARD:
            # Standard Transcribe job
            self.transcribeJobInfo = transcribe_client.get_transcription_job(TranscriptionJobName=job_name)["TranscriptionJob"]
            job_status = self.transcribeJobInfo["TranscriptionJobStatus"]
            if "ContentRedaction" in self.transcribeJobInfo:
                self.transcript_uri = self.transcribeJobInfo["Transcript"]["RedactedTranscriptFileUri"]
            else:
                self.transcript_uri = self.transcribeJobInfo["Transcript"]["TranscriptFileUri"]
        elif self.api_mode == cf.API_ANALYTICS:
            # Call Analytics Transcribe job
            self.transcribeJobInfo = transcribe_client.get_call_analytics_job(CallAnalyticsJobName=job_name)["CallAnalyticsJob"]
            job_status = self.transcribeJobInfo["CallAnalyticsJobStatus"]
            if "RedactedTranscriptFileUri" in self.transcribeJobInfo["Transcript"]:
                self.transcript_uri = self.transcribeJobInfo["Transcript"]["RedactedTranscriptFileUri"]
            else:
                self.transcript_uri = self.transcribeJobInfo["Transcript"]["TranscriptFileUri"]
        else:
            # This should not happen, but will trigger an exception later
            job_status = "UNKNOWN"

        return job_status

    def parse_transcribe_file(self, sf_event):
        """
        Parses the output from the specified Transcribe job
        """
        # Load in the Amazon Transcribe job header information, ensuring that the job has completed
        transcribe = boto3.client("transcribe")
        job_name = sf_event["jobName"]
        try:
            job_status = self.load_transcribe_job_info(sf_event)
            assert job_status == "COMPLETED", f"Transcription job '{job_name}' has not yet completed."
        except transcribe.exceptions.BadRequestException:
            assert False, f"Unable to load information for Transcribe job named '{job_name}'."

        # Create an MP3 playback file if we have to
        self.createPlaybackMP3Audio()

        # Pick out the config parameters that we need
        outputS3Bucket = cf.appConfig[cf.CONF_S3BUCKET_OUTPUT]
        outputS3Key = cf.appConfig[cf.CONF_PREFIX_PARSED_RESULTS]

        # Parse Call GUID and Agent Name/ID from filename if possible
        self.setGUID(job_name)
        self.setAgent(job_name)

        # Work out the conversation time and set the language code
        self.calculateTranscribeConversationTime(job_name)
        self.setComprehendLanguageCode(self.transcribeJobInfo["LanguageCode"])

        # Download the job JSON results file to a local temp file - different Transcribe modes put
        # the files in different folder structures, so just strip everything past the bucket name
        self.jsonOutputFilename = self.transcript_uri.split("/")[-1]
        json_filepath = TMP_DIR + '/' + self.jsonOutputFilename
        transcriptResultsKey = "/".join(self.transcript_uri.split("/")[4:])

        # Now download - this has been known to get a "404 HeadObject Not Found",
        # which makes no sense, so if that happens then re-try in a sec.  Only once.
        s3Client = boto3.client('s3')
        try:
            s3Client.download_file(outputS3Bucket, transcriptResultsKey, json_filepath)
        except:
            time.sleep(3)
            s3Client.download_file(outputS3Bucket, transcriptResultsKey, json_filepath)

        # Before we process, let's load up any required simply entity map
        self.loadSimpleEntityStringMap()

        # Now create turn-by-turn diarisation, with associated sentiments and entities
        self.speechSegmentList = self.createTurnByTurnSegments(json_filepath)
        
        # generate JSON results
        output = self.outputAsJSON()

        # Write out the JSON data to our S3 location
        s3Resource = boto3.resource('s3')
        s3Object = s3Resource.Object(outputS3Bucket, outputS3Key + '/' + self.jsonOutputFilename)
        s3Object.put(
            Body=(bytes(json.dumps(output).encode('UTF-8')))
        )

        # Index transcript in Kendra, if transcript search is enabled
        kendraIndexId = cf.appConfig[cf.CONF_KENDRA_INDEX_ID]
        if (kendraIndexId != "None"):
            analysisUri = f"{cf.appConfig[cf.CONF_WEB_URI]}#parsedFiles/{self.jsonOutputFilename}"
            transcript_with_markers = prepare_transcript(json_filepath)
            conversationAnalytics = output["ConversationAnalytics"]
            put_kendra_document(kendraIndexId, analysisUri, conversationAnalytics, transcript_with_markers)
            
        # Return our filename for re-use later
        return self.jsonOutputFilename


def lambda_handler(event, context):
    # Load our configuration data
    sf_data = copy.deepcopy(event)
    cf.loadConfiguration()

    # Instantiate our parser and write out our processed file
    transcribeParser = TranscribeParser(cf.appConfig[cf.CONF_MINPOSITIVE],
                                        cf.appConfig[cf.CONF_MINNEGATIVE],
                                        cf.appConfig[cf.CONF_ENTITYENDPOINT])
    outputFilename = transcribeParser.parse_transcribe_file(sf_data)

    # Get the object from the event and show its content type
    sf_data["parsedJsonFile"] = outputFilename
    return sf_data


# Main entrypoint for testing
if __name__ == "__main__":
    # Standard test event
    event = {
        "bucket": "ak-cci-input",
        # "key": "originalAudio/mono.wav",
        # "apiMode": "standard",
        # "jobName": "mono.wav",
        "key": "originalAudio/stereo_std.mp3",
        "apiMode": "standard",
        "jobName": "stereo_std.mp3",
        # "key": "originalAudio/stereo.mp3",
        # "apiMode": "analytics",
        # "jobName": "stereo.mp3",
        "langCode": "en-US",
        "transcribeStatus": "COMPLETED"
    }
    lambda_handler(event, "")
