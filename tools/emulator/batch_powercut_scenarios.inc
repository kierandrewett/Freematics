    // Exhaustively cut power at every persisted byte of a three-record
    // appendBatch, then reopen the queue and advance the saved acknowledgement
    // cursor one record at a time. The seed record catches cursor offsets that
    // accidentally work only when a batch begins at byte zero.
    {
        const std::string cutFrames[] = {first, second, "0:1500,10C:920,0D:44,"};
        const char* cutData[] = {cutFrames[0].c_str(), cutFrames[1].c_str(), cutFrames[2].c_str()};
        const uint16_t cutLengths[] = {(uint16_t)cutFrames[0].size(),
                                       (uint16_t)cutFrames[1].size(),
                                       (uint16_t)cutFrames[2].size()};
        bool allCutsPassed = true;
        uint32_t completedCutCount = 0;
        const uint32_t batchBytes = (12 + cutLengths[0]) + (12 + cutLengths[1]) +
                                    (12 + cutLengths[2]);
        for (uint32_t cut = 0; allCutsPassed && cut <= batchBytes; cut++) {
            const char* failedAt = "setup";
            resetCard();
            DurableQueue writer;
            allCutsPassed = writer.begin() && append(writer, first);
            cardPowerCutBudget = cut;
            bool simulatedReset = false;
            try {
                (void)writer.appendBatch(cutData, cutLengths, 3);
            } catch (const std::runtime_error&) {
                simulatedReset = true;
            }
            cardPowerCutBudget = -1;
            sdLockDepth = 0; // volatile RTOS lock state is lost with power
            completedCutCount++;

            uint32_t remainingCutBytes = cut;
            uint32_t completeBatchRecords = 0;
            while (completeBatchRecords < 3 &&
                   remainingCutBytes >= 12 + cutLengths[completeBatchRecords]) {
                remainingCutBytes -= 12 + cutLengths[completeBatchRecords];
                completeBatchRecords++;
            }
            DurableQueue reopened;
            failedAt = "first reopen";
            allCutsPassed = allCutsPassed && simulatedReset && reopened.begin();
            std::vector<std::string> replayed;
            while (allCutsPassed) {
                const std::string frame = peek(reopened);
                if (frame.empty()) break;
                replayed.push_back(frame);
            }
            allCutsPassed = allCutsPassed && replayed.size() == completeBatchRecords + 1 &&
                replayed[0] == first;
            for (uint32_t index = 0; allCutsPassed && index < completeBatchRecords; index++)
                allCutsPassed = replayed[index + 1] == cutFrames[index];
            if (allCutsPassed && reopened.damaged()) {
                failedAt = "recover damaged tail";
                allCutsPassed = reopened.recover();
                replayed.clear();
                reopened.retry();
                while (allCutsPassed) {
                    const std::string frame = peek(reopened);
                    if (frame.empty()) break;
                    replayed.push_back(frame);
                }
                allCutsPassed = allCutsPassed && replayed.size() == completeBatchRecords + 1 &&
                    replayed[0] == first;
                for (uint32_t index = 0; allCutsPassed && index < completeBatchRecords; index++)
                    allCutsPassed = replayed[index + 1] == cutFrames[index];
            }

            // Persist each cursor step, then reopen and prove the next intact
            // record remains at the head. A final incomplete tail is never
            // treated as an acknowledged or complete record.
            for (uint32_t acknowledged = 0; allCutsPassed && acknowledged <= completeBatchRecords;
                 acknowledged++) {
                DurableQueue beforeAck;
                failedAt = "acknowledgement reopen";
                allCutsPassed = beforeAck.begin();
                if (allCutsPassed) {
                    beforeAck.retry();
                    const std::string expected = acknowledged == 0 ? first : cutFrames[acknowledged - 1];
                    allCutsPassed = peek(beforeAck) == expected && beforeAck.acknowledge();
                }
            }
            DurableQueue finalReopen;
            failedAt = "final cursor reopen";
            allCutsPassed = allCutsPassed && finalReopen.begin();
            if (allCutsPassed) {
                finalReopen.retry();
                allCutsPassed = peek(finalReopen).empty();
            }
            if (!allCutsPassed) std::cerr << "appendBatch power-cut case failed at byte " << cut << " during " << failedAt << "\n";
        }
        report("appendBatch reset/reopen at every byte preserves complete records and exact ack cursor",
               allCutsPassed && completedCutCount == batchBytes + 1, true, completedCutCount);
    }
