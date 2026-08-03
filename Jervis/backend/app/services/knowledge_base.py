import logging
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.vector_models import KnowledgeChunk
from app.services.embeddings import embed_text, embed_batch

logger = logging.getLogger(__name__)

TOP_K = 3


KB_CATEGORIES = {
    "services": [
        {"title": "Teeth Cleaning", "content": "Professional teeth cleaning removes plaque and tartar buildup that brushing alone cannot remove. Our hygienists use ultrasonic scaling and polishing for a thorough clean. Recommended every 6 months for optimal oral health. Takes about 45 minutes. Most insurance plans cover this at 100%."},
        {"title": "Teeth Whitening", "content": "Professional teeth whitening uses safe, ADA-approved bleaching agents to lighten tooth color by several shades. We offer both in-office whitening (1 hour) and take-home kits. Results last 6-12 months with proper care. In-office whitening is $120. Take-home kits are $80. Avoid staining foods and drinks for 48 hours after treatment."},
        {"title": "Dental Checkup", "content": "A comprehensive dental checkup includes a thorough examination of teeth, gums, and oral tissues, X-rays if needed, and oral cancer screening. Recommended every 6 months for most patients. Takes about 30 minutes. The checkup costs $30 and includes basic X-rays if needed."},
        {"title": "Dental Filling", "content": "Dental fillings restore teeth damaged by decay. We use tooth-colored composite fillings that match your natural tooth shade. The procedure is completed in one visit and takes about 30-45 minutes. Fillings start at $80 depending on the size and location of the cavity."},
        {"title": "Root Canal", "content": "Root canal treatment removes infected or inflamed pulp from inside the tooth, saving it from extraction. The procedure is performed under local anesthesia and is comfortable. It may require 1-2 visits depending on the complexity. Takes about 60-90 minutes per visit. Cost varies by tooth type, typically $300-$1200."},
        {"title": "Tooth Extraction", "content": "Gentle tooth extraction for damaged, decayed, or problematic teeth. We also offer wisdom teeth removal. Simple extractions take about 15-30 minutes. Surgical extractions may take longer. Local anesthesia is used. Cost ranges from $75-$200 depending on complexity."},
        {"title": "Dental Crown", "content": "Dental crowns restore damaged or weakened teeth to their original shape, size, and strength. We use porcelain and ceramic crowns that match your natural tooth color. The procedure typically requires two visits. Takes about 60 minutes per visit. Cost is $800-$1500 per crown."},
        {"title": "Dental Implant", "content": "Dental implants replace missing teeth with a permanent artificial root and crown. The implant is surgically placed into the jawbone and fuses with the bone over several months. Requires 2-3 visits over 3-6 months. Cost is $1500-$3000 per implant including the crown."},
        {"title": "Braces", "content": "Orthodontic braces straighten teeth and correct bite issues. We offer traditional metal braces and clear ceramic braces. Treatment duration varies from 6 months to 2 years depending on the case. Available for all ages. Consultation is free. Treatment costs range from $3000-$7000."},
        {"title": "Veneers", "content": "Dental veneers are thin porcelain shells bonded to the front of teeth to improve appearance. They fix chips, gaps, discoloration, and misalignment. Requires 2-3 visits. Cost is $500-$1500 per veneer. Results are natural-looking and long-lasting."},
        {"title": "Dentures", "content": "Partial and full dentures replace missing teeth and restore your smile. We offer custom-made dentures that fit comfortably and look natural. The process takes 2-4 visits over several weeks. Cost ranges from $300-$1500 per denture depending on the type."},
        {"title": "Gum Treatment", "content": "Periodontal (gum) treatment addresses gum disease ranging from early gingivitis to advanced periodontitis. Treatments include deep cleaning (scaling and root planing), gum surgery, and maintenance care. Cost varies from $200-$1000 depending on the severity and treatment needed."},
    ],
    "pricing": [
        {"title": "Teeth Cleaning Price", "content": "Professional teeth cleaning is $50. Most dental insurance plans cover preventive cleanings at 100% with no out-of-pocket cost. We accept all major insurance providers and will verify your coverage before your visit."},
        {"title": "Whitening Price", "content": "In-office professional whitening is $120. Take-home whitening kits are $80. Results from professional whitening are significantly better and safer than over-the-counter products. We offer a free consultation to determine the best option for you."},
        {"title": "Checkup Price", "content": "Dental checkup is $30. This includes a comprehensive examination and basic X-rays if needed. Regular checkups help catch problems early when they are easier and less expensive to treat."},
        {"title": "Filling Price", "content": "Tooth-colored fillings start at $80 depending on the size and location of the cavity. We use composite resin that matches your natural tooth color. Insurance typically covers a portion of the cost."},
        {"title": "Root Canal Price", "content": "Root canal treatment costs $300-$1200 depending on the tooth. Front teeth are less expensive, while molars cost more due to their complexity. Most insurance plans cover a significant portion of root canal treatment."},
        {"title": "Extraction Price", "content": "Simple extractions start at $75. Surgical extractions (such as wisdom teeth) range from $150-$300. We offer payment plans for extensive procedures. Insurance may cover extractions depending on your plan."},
        {"title": "Crown Price", "content": "Dental crowns cost $800-$1500 per tooth. Porcelain and ceramic crowns are priced at the higher end. We offer a 5-year warranty on all crowns. Insurance typically covers 50-80% of the cost."},
        {"title": "Implant Price", "content": "Dental implants cost $1500-$3000 per tooth including the crown. This includes the implant fixture, abutment, and custom crown. We offer financing options. Insurance coverage varies."},
        {"title": "Payment Methods", "content": "We accept cash, credit cards (Visa, MasterCard, American Express), debit cards, and most dental insurance plans. Payment is due at the time of service unless other arrangements have been made. We offer flexible payment plans for major procedures."},
        {"title": "Insurance", "content": "We accept most major dental insurance plans including Delta Dental, MetLife, Cigna, Aetna, and Blue Cross Blue Shield. Please bring your insurance card to your appointment. We can verify your coverage and benefits beforehand at no charge."},
        {"title": "Financing", "content": "We offer in-house financing for treatments over $500. No-interest payment plans are available for qualified patients. We also accept CareCredit and Lending Club financing for larger procedures."},
    ],
    "hours": [
        {"title": "Clinic Hours Monday-Friday", "content": "Monday through Friday: 9 AM to 6 PM. Our front desk is available from 8:30 AM to help with check-ins and questions. We recommend arriving 10-15 minutes before your scheduled appointment."},
        {"title": "Clinic Hours Saturday", "content": "Saturday: 10 AM to 4 PM. Saturday appointments are available for checkups, cleanings, and urgent care. Limited availability, so booking in advance is recommended."},
        {"title": "Clinic Closed Sunday", "content": "We are closed on Sundays. Emergency patients can call our emergency line for after-hours guidance. If you have a dental emergency on Sunday, please call and follow the prompts."},
        {"title": "Emergency Appointments", "content": "Emergency appointments are available on the same day during business hours. Call early in the morning for the best availability. For after-hours emergencies, call our emergency line and follow the instructions."},
        {"title": "Booking Hours", "content": "You can book appointments by phone during business hours (Mon-Fri 8:30 AM - 5:30 PM, Sat 10 AM - 3 PM). You can also book online anytime through our website or by sending a message."},
    ],
    "location": [
        {"title": "Clinic Address", "content": "We are located at 123 Main Street, Dhaka, Bangladesh. Our clinic is on the ground floor of the Dhaka Medical Plaza building. Free parking is available on-site in the rear parking lot."},
        {"title": "Getting Here by Car", "content": "From the city center, take Main Street heading east. The clinic is on the right side, just past the Dhaka Medical College intersection. Free parking is available in our lot behind the building."},
        {"title": "Getting Here by Public Transit", "content": "We are centrally located in Dhaka with easy access by bus and metro. The nearest bus stop is Dhaka Medical College stop, about a 2-minute walk. Several bus routes serve this area."},
        {"title": "Wheelchair Access", "content": "Our clinic is fully wheelchair accessible. The entrance is on the ground floor with a ramp. Our waiting area and treatment rooms are all on the ground floor. Accessible restrooms are available."},
        {"title": "Parking", "content": "Free on-site parking is available for all patients. The parking lot is located behind the building. Enter from the alley next to the building. There is also street parking available on Main Street."},
        {"title": "Landmarks", "content": "We are located next to Dhaka Medical College and Hospital. Look for the Dhaka Medical Plaza building. Our clinic is on the ground floor, Suite 101."},
    ],
    "policies": [
        {"title": "Cancellation Policy", "content": "You can cancel or reschedule your appointment up to 2 hours before the scheduled time at no charge. Cancellations with less than 2 hours notice may incur a $25 fee. No-shows will be charged $50. We understand emergencies happen and will work with you."},
        {"title": "Rescheduling Policy", "content": "You can reschedule your appointment up to 2 hours before the scheduled time without penalty. Please call us as early as possible so we can offer your slot to another patient. We will do our best to accommodate your preferred new time."},
        {"title": "New Patient Forms", "content": "New patients should arrive 15 minutes before their scheduled appointment to complete registration forms. You can also fill out the forms online at our website before your visit to save time. Please bring a valid ID and your insurance card."},
        {"title": "Insurance Policy", "content": "We accept most major dental insurance plans. We will verify your coverage and benefits before your first visit. You are responsible for any co-payments or deductibles at the time of service. We will file claims on your behalf."},
        {"title": "Payment Policy", "content": "Payment is due at the time of service unless other arrangements have been made. We accept cash, credit/debit cards, and insurance payments. For treatments not covered by insurance, we offer upfront cost estimates and flexible payment plans."},
        {"title": "Privacy Policy", "content": "We are committed to protecting your privacy. All patient information is kept confidential in accordance with applicable laws. Your dental records are stored securely and are only accessible to authorized staff involved in your care."},
        {"title": "No-Show Policy", "content": "Patients who miss their appointment without 2 hours notice are considered no-shows. A $50 fee applies to no-shows. Three no-shows may result in being unable to book future appointments without prepayment."},
    ],
    "faq": [
        {"title": "How often should I visit the dentist?", "content": "We recommend visiting every 6 months for a checkup and cleaning. More frequent visits may be needed if you have gum disease, a history of cavities, or other dental conditions. Your dentist will recommend the best schedule for you."},
        {"title": "Is teeth whitening safe?", "content": "Yes, professional teeth whitening is safe when performed by a dental professional. We use ADA-approved bleaching agents and monitor your comfort throughout the procedure. Sensitivity is temporary and manageable."},
        {"title": "Do you accept walk-ins?", "content": "We accept walk-ins for dental emergencies during business hours. For routine appointments, we recommend booking in advance to ensure availability. Walk-ins are welcome but may have a longer wait time."},
        {"title": "What should I bring to my first appointment?", "content": "Bring a valid photo ID, your insurance card, and any previous dental records or X-rays if you have them. Also bring a list of any medications you are currently taking. Arrive 15 minutes early to complete registration."},
        {"title": "Do you offer pediatric dentistry?", "content": "Yes, we welcome patients of all ages. We recommend bringing children for their first dental visit by age 1 or within 6 months of their first tooth erupting. Our team is experienced in making dental visits comfortable for children."},
        {"title": "What is your cancellation fee?", "content": "Cancellations with less than 2 hours notice may incur a $25 fee. No-shows are charged $50. We understand that emergencies happen, so please call us as soon as possible if you need to cancel or reschedule."},
        {"title": "How long does a dental cleaning take?", "content": "A routine dental cleaning takes about 45 minutes. If you have not had a cleaning in a while or have significant tartar buildup, it may take a bit longer. We always prioritize your comfort."},
        {"title": "Does whitening hurt?", "content": "Professional whitening is generally comfortable. Some patients experience temporary sensitivity during or after the procedure, which typically subsides within 24-48 hours. We use desensitizing agents to minimize discomfort."},
        {"title": "How long do fillings last?", "content": "Tooth-colored composite fillings typically last 5-10 years with proper care. The longevity depends on the size of the filling, your oral hygiene habits, and whether you grind your teeth. Regular checkups help us monitor the condition of your fillings."},
        {"title": "Can I get a crown in one visit?", "content": "Yes, we offer same-day crowns using CEREC technology. This means you can get your crown designed, milled, and placed all in a single visit. Ask us if you are a candidate for same-day crowns."},
        {"title": "What is gum disease?", "content": "Gum disease (periodontal disease) is an infection of the tissues that support your teeth. It is caused by plaque buildup and can lead to tooth loss if untreated. Early stages (gingivitis) are reversible with proper care. Advanced stages require professional treatment."},
        {"title": "How do I know if I have a cavity?", "content": "Symptoms of a cavity include toothache, tooth sensitivity, pain when eating or drinking, and visible holes or pits in your teeth. However, cavities can be present without symptoms, which is why regular checkups and X-rays are important for early detection."},
        {"title": "Are dental X-rays safe?", "content": "Yes, dental X-rays are very safe. The radiation exposure from dental X-rays is extremely low. We use digital X-rays which require even less radiation than traditional film X-rays. We only take X-rays when necessary for diagnosis."},
        {"title": "Can I get my teeth cleaned during pregnancy?", "content": "Yes, dental cleanings are safe and recommended during pregnancy. Hormonal changes during pregnancy can increase the risk of gum disease. We recommend continuing regular dental care throughout pregnancy. Please let us know if you are pregnant."},
    ],
    "emergency": [
        {"title": "Dental Emergency - What to Do", "content": "For dental emergencies, call us immediately during business hours. After hours, call our emergency line and follow the prompts. Common dental emergencies include: knocked-out tooth, cracked or broken tooth, severe toothache, abscess or swelling, lost filling or crown, and bleeding that won't stop."},
        {"title": "Knocked-Out Tooth", "content": "If a tooth is knocked out, handle it by the crown (top), not the root. Rinse it gently with water if dirty. Try to reinsert it into the socket if possible. If not, place it in a container of milk or saliva. Come to the clinic immediately — time is critical for saving the tooth."},
        {"title": "Severe Toothache", "content": "For severe toothache, rinse your mouth with warm water and floss gently to remove any trapped food. Apply a cold compress to the outside of your cheek. Take over-the-counter pain reliever if needed. Call us immediately for an emergency appointment."},
        {"title": "Broken or Chipped Tooth", "content": "For a broken or chipped tooth, save any broken pieces. Rinse your mouth with warm water. Apply a cold compress to reduce swelling. If the tooth is painful, take over-the-counter pain reliever. Come to the clinic as soon as possible."},
        {"title": "Lost Filling or Crown", "content": "If a filling or crown comes out, keep it if you can. Apply dental cement or sugar-free gum to the sensitive area as a temporary measure. Avoid chewing on that side. Call us to schedule a repair appointment as soon as possible."},
        {"title": "Abscess or Swelling", "content": "An abscess or swelling in the mouth is a serious condition that requires immediate attention. Rinse with warm salt water and take over-the-counter pain reliever. Come to the clinic immediately or go to the emergency room if swelling is severe or you have difficulty breathing."},
    ],
}


class KnowledgeBaseService:
    """Manages knowledge chunks with vector search for RAG."""

    def __init__(self, db: AsyncSession, tenant_id: str):
        self.db = db
        self.tenant_id = tenant_id

    async def add_chunk(
        self, category: str, title: str, content: str, chunk_metadata: dict = None
    ) -> KnowledgeChunk:
        chunk = KnowledgeChunk(
            tenant_id=self.tenant_id,
            category=category,
            title=title,
            content=content,
            chunk_metadata=chunk_metadata or {},
        )
        embedding = embed_text(content)
        if embedding:
            chunk.embedding = embedding
        self.db.add(chunk)
        await self.db.flush()
        return chunk

    async def search(
        self, query: str, category: Optional[str] = None, top_k: int = TOP_K
    ) -> List[KnowledgeChunk]:
        query_embedding = embed_text(query)
        if query_embedding is None:
            return []

        stmt = select(KnowledgeChunk).where(
            KnowledgeChunk.tenant_id == self.tenant_id
        )
        if category:
            stmt = stmt.where(KnowledgeChunk.category == category)

        results = await self.db.execute(stmt)
        chunks = list(results.scalars().all())

        if not chunks:
            return []

        # Simple cosine similarity using pgvector if embeddings exist
        # Fall back to keyword matching if no embeddings
        chunks_with_embeddings = [c for c in chunks if c.embedding]
        if chunks_with_embeddings and len(chunks_with_embeddings) >= 1:
            try:
                # Use pgvector cosine distance ordering
                from sqlalchemy import func as sql_func

                query_vec = query_embedding
                stmt = (
                    select(KnowledgeChunk)
                    .where(
                        KnowledgeChunk.tenant_id == self.tenant_id,
                        KnowledgeChunk.id.in_([c.id for c in chunks_with_embeddings]),
                    )
                    .order_by(
                        sql_func.cosine_distance(
                            KnowledgeChunk.embedding, query_vec
                        ).asc()
                    )
                    .limit(top_k)
                )
                results = await self.db.execute(stmt)
                return list(results.scalars().all())
            except Exception:
                logger.warning("pgvector search failed, falling back to keyword search")

        # Fallback: keyword matching
        return self._keyword_search(query, chunks, top_k)

    def _keyword_search(
        self, query: str, chunks: List[KnowledgeChunk], top_k: int
    ) -> List[KnowledgeChunk]:
        query_lower = query.lower()
        scored = []
        for chunk in chunks:
            score = 0
            content_lower = chunk.content.lower()
            title_lower = chunk.title.lower()
            for word in query_lower.split():
                if word in content_lower:
                    score += 2
                if word in title_lower:
                    score += 3
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:top_k]]

    async def seed_from_dict(self, categories: dict):
        """Seed the KB from a category → list of {title, content} dicts."""
        for category, items in categories.items():
            for item in items:
                await self.add_chunk(
                    category=category,
                    title=item["title"],
                    content=item["content"],
                )
        await self.db.commit()
        logger.info("Seeded knowledge base with %d chunks", len(categories))

    async def rebuild_index(self):
        """Re-embed all chunks for this tenant."""
        stmt = select(KnowledgeChunk).where(
            KnowledgeChunk.tenant_id == self.tenant_id
        )
        results = await self.db.execute(stmt)
        chunks = list(results.scalars().all())

        for chunk in chunks:
            embedding = embed_text(chunk.content)
            if embedding:
                chunk.embedding = embedding

        await self.db.commit()
        logger.info("Rebuilt embeddings for %d chunks", len(chunks))